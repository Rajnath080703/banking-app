from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Account, Customer, Transaction
from ..security import activity, audit, branch_scope, ensure_branch_access, once_per_form, require_perm
from ..services import (TransferError, create_approval, needs_large_txn_approval, pending_request,
                        post_transaction, reversal_problem, search_customers, transfer_funds, transfer_problem)
from ..utils import date_range_from_request, day_bounds, parse_amount, inr

bp = Blueprint("transactions", __name__, url_prefix="/transactions")


@bp.route("/")
@login_required
@require_perm("transactions", "view")
def index():
    start, end, rkey = date_range_from_request("week")
    s, e = day_bounds(start, end)
    q = branch_scope(Transaction.query, Transaction).filter(Transaction.timestamp.between(s, e))
    ttype = request.args.get("type", "")
    channel = request.args.get("channel", "")
    term = request.args.get("q", "").strip()
    if ttype:
        q = q.filter(Transaction.txn_type == ttype)
    if channel:
        q = q.filter(Transaction.channel == channel)
    if request.args.get("flagged"):
        q = q.filter(Transaction.flagged.is_(True))
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(Transaction.customer_id.in_(ids), Transaction.txn_ref == term.upper()))
    totals = q.with_entities(Transaction.txn_type, db.func.sum(Transaction.amount), db.func.count()) \
        .group_by(Transaction.txn_type).all()
    page = request.args.get("page", 1, type=int)
    txns = q.order_by(Transaction.timestamp.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template("transactions/index.html", txns=txns, start=start, end=end, rkey=rkey,
                           ttype=ttype, channel=channel, term=term,
                           totals={t: (amt, n) for t, amt, n in totals})


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("transactions", "create")
@once_per_form
def new():
    account = None
    acc_no = request.values.get("account", "").strip()
    if acc_no:
        account = Account.query.filter_by(account_number=acc_no).first()
        if account:
            ensure_branch_access(account)
        else:
            flash("Account not found.", "warning")
    def form_error(message):
        """Show the problem on the same form, keeping what the employee typed."""
        db.session.rollback()
        flash(message, "danger")
        return render_template("transactions/new.html", account=account, acc_no=acc_no, recent=_recent(account),
                               f=request.form)

    if request.method == "POST" and account:
        ttype = request.form.get("txn_type")
        amount = parse_amount(request.form.get("amount"))
        channel = request.form.get("channel", "Cash")
        if channel not in ("Cash", "Cheque", "NEFT", "IMPS", "UPI", "Transfer"):
            channel = "Cash"
        description = (request.form.get("description") or "").strip()
        if ttype not in ("Credit", "Debit") or amount is None:
            return form_error("Choose deposit or withdrawal and enter an amount between ₹0.01 and ₹1,000 crore, "
                              "with at most two decimals.")
        if needs_large_txn_approval(amount):
            if ttype == "Debit" and amount > account.balance:
                return form_error(f"Insufficient balance: available {inr(account.balance, 2)}.")
            req = create_approval("large_txn", "account", account.id, account.customer, amount,
                                  f"{'Deposit' if ttype == 'Credit' else 'Withdrawal'} of {inr(amount, 2)} on {account.account_number}",
                                  {"txn_type": ttype, "channel": channel, "description": description})
            db.session.commit()
            flash(f"{inr(amount, 2)} is above the high-value limit, so it was sent for maker-checker approval "
                  f"({req.request_number}). It will post once a checker and a manager approve it.", "warning")
            return redirect(url_for("transactions.new", account=account.account_number))
        try:
            txn = post_transaction(account, ttype, amount, channel,
                                   "Deposit" if ttype == "Credit" else "Withdrawal",
                                   description or ("Cash deposit" if ttype == "Credit" else "Cash withdrawal"))
        except ValueError as exc:
            return form_error(str(exc))
        activity(account.customer_id, "Payment received" if ttype == "Credit" else "Withdrawal",
                 txn.txn_ref, f"{inr(amount, 2)} via {channel}")
        audit("Transaction posted", "transactions", txn.txn_ref, f"{ttype} {inr(amount, 2)} on {account.account_number}")
        db.session.commit()
        msg = f"Transaction {txn.txn_ref} posted. New balance {inr(account.balance, 2)}."
        if txn.flagged:
            msg += " It was flagged by a risk rule and routed for review."
        flash(msg, "warning" if txn.flagged else "success")
        return redirect(url_for("transactions.new", account=account.account_number))
    return render_template("transactions/new.html", account=account, acc_no=acc_no, recent=_recent(account), f={})


def _recent(account):
    if not account:
        return []
    return Transaction.query.filter_by(account_id=account.id).order_by(Transaction.timestamp.desc()).limit(8).all()


@bp.route("/<int:tid>/reverse", methods=["POST"])
@login_required
@require_perm("transactions", "create")
@once_per_form
def reverse(tid):
    """Maker raises a reversal of a wrongly posted teller transaction. Nothing changes until a
    checker and a manager approve it (services._exec_txn_reversal)."""
    txn = db.session.get(Transaction, tid) or abort(404)
    ensure_branch_access(txn.account)
    back = url_for("customers.profile", cid=txn.customer_id, tab="transactions")
    reason = (request.form.get("reason") or "").strip()[:200]
    if len(reason) < 5:
        flash("Enter the reason for the reversal (at least 5 characters).", "danger")
        return redirect(back)
    problem = reversal_problem(txn)
    if problem:
        flash(f"Cannot request a reversal: {problem}", "danger")
        return redirect(back)
    open_req = pending_request("txn_reversal", txn.id)
    if open_req:
        flash(f"A reversal of {txn.txn_ref} is already waiting for approval ({open_req.request_number}).", "warning")
        return redirect(back)
    req = create_approval("txn_reversal", "transaction", txn.id, txn.customer, txn.amount,
                          f"Reverse {txn.txn_type.lower()} {txn.txn_ref} of {inr(txn.amount, 2)} on {txn.account.account_number}",
                          {"reason": reason, "txn_ref": txn.txn_ref})
    db.session.commit()
    flash(f"Reversal of {txn.txn_ref} sent for approval ({req.request_number}). The balance changes only when it is approved.",
          "success")
    return redirect(back)


def _account_in_scope(number):
    """An account the employee may act on, by exact number; None if unknown or outside their branch."""
    number = (number or "").strip()[:20]
    if not number:
        return None
    acc = Account.query.filter_by(account_number=number).first()
    if acc and not (current_user.sees_all_branches or acc.branch_id == current_user.branch_id):
        return None  # same answer as "not found": no hint that it exists in another branch
    return acc


@bp.route("/transfer", methods=["GET", "POST"])
@login_required
@require_perm("transactions", "create")
@once_per_form
def transfer():
    """Move money between two accounts in this bank (both in the employee's branch scope)."""
    from_no, to_no = request.values.get("from", "").strip()[:20], request.values.get("to", "").strip()[:20]
    src, dst = _account_in_scope(from_no), _account_in_scope(to_no)
    missing = [n for n, a in ((from_no, src), (to_no, dst)) if n and not a]

    def show(f=None):
        return render_template("transactions/transfer.html", src=src, dst=dst, from_no=from_no, to_no=to_no,
                               missing=missing, f=f or {})

    if request.method == "POST":
        amount = parse_amount(request.form.get("amount"))
        narration = (request.form.get("narration") or "").strip()[:150]
        problem = ("Enter an amount between ₹0.01 and ₹1,000 crore, with at most two decimals." if amount is None
                   else transfer_problem(src, dst, amount))
        if problem:
            flash(problem, "danger")
            return show(request.form)
        if needs_large_txn_approval(amount):
            req = create_approval("fund_transfer", "account", src.id, src.customer, amount,
                                  f"Transfer {inr(amount, 2)} from {src.account_number} to {dst.account_number}",
                                  {"to_account_id": dst.id, "to_account": dst.account_number, "narration": narration})
            db.session.commit()
            flash(f"{inr(amount, 2)} is above the high-value limit, so the transfer was sent for maker-checker approval "
                  f"({req.request_number}). Nothing moves until it is approved.", "warning")
            return redirect(url_for("transactions.transfer", **{"from": src.account_number, "to": dst.account_number}))
        try:
            out, into = transfer_funds(src, dst, amount, narration)
        except (TransferError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return show(request.form)
        audit("Transfer posted", "transactions", out.txn_ref,
              f"{inr(amount, 2)} {src.account_number} -> {dst.account_number} ({into.txn_ref})")
        db.session.commit()
        flash(f"Transferred {inr(amount, 2)} from {src.account_number} to {dst.account_number} "
              f"({out.txn_ref} / {into.txn_ref}). New balances {inr(src.balance, 2)} and {inr(dst.balance, 2)}.",
              "warning" if (out.flagged or into.flagged) else "success")
        return redirect(url_for("transactions.transfer", **{"from": src.account_number, "to": dst.account_number}))
    return show()
