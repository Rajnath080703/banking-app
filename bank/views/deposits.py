from decimal import Decimal

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from ..extensions import db
from ..models import Account, Customer, FixedDeposit
from ..security import once_per_form, customer_from_request, branch_scope, ensure_branch_access, require_perm
from ..ledger import TERM_DEPOSITS
from ..private_search import term as search_term
from ..services import create_approval, pending_request, post_transaction, premature_quote, search_customers
from ..utils import add_months, fd_maturity, parse_amount, unique_number, local_today, inr

bp = Blueprint("deposits", __name__, url_prefix="/deposits")

FD_RATES = {6: Decimal("6.00"), 12: Decimal("6.80"), 24: Decimal("7.00"), 36: Decimal("7.10"), 60: Decimal("6.50")}


@bp.route("/")
@login_required
@require_perm("deposits", "view")
def index():
    q = branch_scope(FixedDeposit.query, FixedDeposit)
    status = request.args.get("status", "")
    term = search_term()
    if status:
        q = q.filter(FixedDeposit.status == status)
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(FixedDeposit.customer_id.in_(ids), FixedDeposit.fd_number == term.upper()))
    if request.args.get("maturing"):
        today = local_today()
        q = q.filter(FixedDeposit.status == "Active", FixedDeposit.maturity_date <= add_months(today, 1))
    page = request.args.get("page", 1, type=int)
    fds = q.order_by(FixedDeposit.start_date.desc()).paginate(page=page, per_page=25, error_out=False)
    base = branch_scope(FixedDeposit.query, FixedDeposit)
    stats = dict(base.with_entities(FixedDeposit.status, func.sum(FixedDeposit.principal)).group_by(FixedDeposit.status).all())
    casa = branch_scope(Account.query, Account).filter(Account.status != "Closed").with_entities(
        Account.account_type, func.sum(Account.balance)).group_by(Account.account_type).all()
    return render_template("deposits/index.html", fds=fds, status=status, term=term, stats=stats, casa=dict(casa))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("deposits", "create")
@once_per_form
def new():
    customer = customer_from_request()

    def form_error(message):
        db.session.rollback()
        flash(message, "danger")
        return render_template("deposits/new.html", customer=customer, rates=FD_RATES, f=request.form)

    if request.method == "POST" and customer:
        principal = parse_amount(request.form.get("principal"), minimum=1000)
        months = request.form.get("tenure_months", type=int)
        acct = db.session.get(Account, request.form.get("account_id", type=int) or 0)
        if principal is None or months not in FD_RATES:
            return form_error("Enter an amount from ₹1,000 (at most two decimals) and choose one of the listed tenures.")
        if not acct or acct.customer_id != customer.id or acct.status != "Active":
            return form_error("Select one of the customer's active accounts to fund the FD.")
        rate = FD_RATES[months]
        start = local_today()
        try:
            post_transaction(acct, "Debit", principal, "Transfer", "FD Booking", "Fixed deposit booking",
                             contra=TERM_DEPOSITS)
        except ValueError as exc:
            return form_error(str(exc))
        fd = FixedDeposit(account_id=acct.id, fd_number=unique_number(FixedDeposit, "fd_number", "FD", 8), customer_id=customer.id,
                          branch_id=customer.branch_id, principal=principal, interest_rate=rate,
                          tenure_months=months, start_date=start, maturity_date=add_months(start, months),
                          maturity_amount=fd_maturity(principal, rate, months), status="Pending")
        db.session.add(fd)
        db.session.flush()
        req = create_approval("fd_create", "fd", fd.id, customer, principal,
                              f"FD of {inr(principal, 2)} for {months} months — {customer.name}",
                              {"funding_account_id": acct.id})
        db.session.commit()
        flash(f"FD {fd.fd_number} booked and sent for approval ({req.request_number}).", "success")
        return redirect(url_for("customers.profile", cid=customer.id, tab="deposits"))
    return render_template("deposits/new.html", customer=customer, rates=FD_RATES, f={})


@bp.route("/<int:fid>/close", methods=["GET", "POST"])
@login_required
@require_perm("deposits", "edit")
@once_per_form
def close(fid):
    """Premature closure: quote at (contracted rate − penalty) for the days run; paid out on approval."""
    from ..models import FixedDeposit as FD
    fd = ensure_branch_access(db.session.get(FD, fid))
    if fd.status != "Active":
        flash("Only an active FD can be closed early.", "danger")
        return redirect(url_for("customers.profile", cid=fd.customer_id, tab="deposits"))
    quote = premature_quote(fd)
    pending = pending_request("fd_close", fd.id)
    if request.method == "POST":
        reason = (request.form.get("reason") or "").strip()[:200]
        if pending:
            flash("A closure request for this FD is already waiting for approval.", "warning")
        elif not reason:
            flash("Give the reason the customer is closing the FD early.", "danger")
        elif not fd.account or fd.account.status in ("Closed", "Rejected", "Pending"):
            flash("The FD has no open payout account. Link one before closing.", "danger")
        else:
            req = create_approval("fd_close", "fd", fd.id, fd.customer, quote["value"],
                                  f"Close FD {fd.fd_number} early: pay {inr(quote['value'], 2)} — {reason}",
                                  {"reason": reason, "quoted_value": str(quote["value"])})
            db.session.commit()
            flash(f"Early closure sent for approval ({req.request_number}). The payout is recalculated on the "
                  f"approval date.", "success")
            return redirect(url_for("customers.profile", cid=fd.customer_id, tab="deposits"))
    return render_template("deposits/close.html", fd=fd, quote=quote, pending=pending)
