from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from ..extensions import db
from ..models import Account, Customer, FixedDeposit
from ..security import customer_from_request, branch_scope, ensure_branch_access, require_perm
from ..services import create_approval, post_transaction, search_customers
from ..utils import add_months, fd_maturity, parse_float, unique_number

bp = Blueprint("deposits", __name__, url_prefix="/deposits")

FD_RATES = {6: 6.0, 12: 6.8, 24: 7.0, 36: 7.1, 60: 6.5}


@bp.route("/")
@login_required
@require_perm("deposits", "view")
def index():
    q = branch_scope(FixedDeposit.query, FixedDeposit)
    status = request.args.get("status", "")
    term = request.args.get("q", "").strip()
    if status:
        q = q.filter(FixedDeposit.status == status)
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(FixedDeposit.customer_id.in_(ids), FixedDeposit.fd_number == term.upper()))
    if request.args.get("maturing"):
        today = date.today()
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
def new():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        principal = parse_float(request.form.get("principal"), 0)
        months = request.form.get("tenure_months", type=int)
        acct = db.session.get(Account, request.form.get("account_id", type=int) or 0)
        if principal < 1000 or months not in FD_RATES:
            flash("Minimum deposit is ₹1,000 and tenure must be one of the listed options.", "danger")
            return redirect(request.url)
        if not acct or acct.customer_id != customer.id:
            flash("Select the customer's funding account.", "danger")
            return redirect(request.url)
        rate = FD_RATES[months]
        start = date.today()
        try:
            post_transaction(acct, "Debit", principal, "Transfer", "FD Booking", "Fixed deposit booking")
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(request.url)
        fd = FixedDeposit(fd_number=unique_number(FixedDeposit, "fd_number", "FD", 8), customer_id=customer.id,
                          branch_id=customer.branch_id, principal=principal, interest_rate=rate,
                          tenure_months=months, start_date=start, maturity_date=add_months(start, months),
                          maturity_amount=fd_maturity(principal, rate, months), status="Pending")
        db.session.add(fd)
        db.session.flush()
        req = create_approval("fd_create", "fd", fd.id, customer, principal,
                              f"FD of ₹{principal:,.0f} for {months} months — {customer.name}",
                              {"funding_account_id": acct.id})
        db.session.commit()
        flash(f"FD {fd.fd_number} booked and sent for approval ({req.request_number}).", "success")
        return redirect(url_for("customers.profile", cid=customer.id, tab="deposits"))
    return render_template("deposits/new.html", customer=customer, rates=FD_RATES)
