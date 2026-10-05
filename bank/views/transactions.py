from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from ..extensions import db
from ..models import Account, Customer, Transaction
from ..security import activity, audit, branch_scope, ensure_branch_access, require_perm
from ..services import post_transaction, search_customers
from ..utils import date_range_from_request, day_bounds, parse_float

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
def new():
    account = None
    acc_no = request.values.get("account", "").strip()
    if acc_no:
        account = Account.query.filter_by(account_number=acc_no).first()
        if account:
            ensure_branch_access(account)
        else:
            flash("Account not found.", "warning")
    if request.method == "POST" and account:
        ttype = request.form.get("txn_type")
        amount = parse_float(request.form.get("amount"), 0)
        channel = request.form.get("channel", "Cash")
        if ttype not in ("Credit", "Debit") or amount <= 0:
            flash("Choose deposit or withdrawal and enter a positive amount.", "danger")
            return redirect(request.url)
        try:
            txn = post_transaction(account, ttype, amount, channel,
                                   "Deposit" if ttype == "Credit" else "Withdrawal",
                                   request.form.get("description") or ("Cash deposit" if ttype == "Credit" else "Cash withdrawal"))
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(request.url)
        activity(account.customer_id, "Payment received" if ttype == "Credit" else "Withdrawal",
                 txn.txn_ref, f"₹{amount:,.2f} via {channel}")
        audit("Transaction posted", "transactions", txn.txn_ref, f"{ttype} ₹{amount:,.2f} on {account.account_number}")
        db.session.commit()
        msg = f"Transaction {txn.txn_ref} posted. New balance ₹{account.balance:,.2f}."
        if txn.flagged:
            msg += " It was flagged by a risk rule and routed for review."
        flash(msg, "warning" if txn.flagged else "success")
        return redirect(url_for("transactions.new", account=account.account_number))
    recent = Transaction.query.filter_by(account_id=account.id).order_by(Transaction.timestamp.desc()).limit(8).all() if account else []
    return render_template("transactions/new.html", account=account, acc_no=acc_no, recent=recent)
