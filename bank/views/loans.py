from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import LOAN_TYPE_LABELS, Account, ApprovalRequest, Customer, Loan, LoanRepayment
from ..security import customer_from_request, activity, audit, branch_scope, ensure_branch_access, require_perm
from ..services import create_approval, money_sum, post_transaction, search_customers
from ..utils import add_months, calc_emi, parse_float, unique_number

bp = Blueprint("loans", __name__, url_prefix="/loans")

FILTERS = [("all", "All Loans"), ("Active", "Active"), ("Closed", "Closed"),
           ("Overdue", "Overdue"), ("Pending", "Pending Approval")]


def _loan_list(loan_type=None, template="loans/index.html", **extra):
    q = branch_scope(Loan.query, Loan)
    if loan_type:
        q = q.filter(Loan.loan_type == loan_type)
    status = request.args.get("status", "all")
    if status != "all":
        q = q.filter(Loan.status == status)
    term = request.args.get("q", "").strip()
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(Loan.customer_id.in_(ids), Loan.loan_number == term.upper()))
    page = request.args.get("page", 1, type=int)
    loans = q.order_by(Loan.created_at.desc()).paginate(page=page, per_page=25, error_out=False)

    base = branch_scope(Loan.query, Loan)
    if loan_type:
        base = base.filter(Loan.loan_type == loan_type)
    counts = dict(base.with_entities(Loan.status, func.count()).group_by(Loan.status).all())
    counts["all"] = sum(counts.values())
    ids_q = base.with_entities(Loan.id)
    stats = {
        "sanctioned": base.filter(Loan.disbursed_on != None).with_entities(func.coalesce(func.sum(Loan.amount), 0)).scalar(),  # noqa
        "outstanding": base.filter(Loan.status.in_(["Active", "Overdue"])).with_entities(func.coalesce(func.sum(Loan.outstanding), 0)).scalar(),
        "overdue": base.filter(Loan.status == "Overdue").with_entities(func.coalesce(func.sum(Loan.outstanding), 0)).scalar(),
        "collected_month": money_sum(LoanRepayment.amount, LoanRepayment.loan_id.in_(ids_q),
                                     LoanRepayment.paid_on >= date.today().replace(day=1)),
    }
    return render_template(template, loans=loans, status=status, term=term, counts=counts,
                           stats=stats, filters=FILTERS, loan_type=loan_type,
                           type_label=LOAN_TYPE_LABELS.get(loan_type, "All Loans"), **extra)


@bp.route("/")
@login_required
@require_perm("loans", "view")
def index():
    return _loan_list()


@bp.route("/type/<loan_type>")
@login_required
@require_perm("loans", "view")
def by_type(loan_type):
    if loan_type not in LOAN_TYPE_LABELS:
        abort(404)
    if loan_type == "gold":
        return redirect(url_for("loans.gold", **request.args))
    return _loan_list(loan_type)


@bp.route("/gold")
@login_required
@require_perm("loans", "view")
def gold():
    term = request.args.get("q", "").strip()
    gold_stats = branch_scope(Loan.query, Loan).filter(
        Loan.loan_type == "gold", Loan.status.in_(["Active", "Overdue"])).with_entities(
        func.coalesce(func.sum(Loan.gold_weight_grams), 0), func.count()).one()
    found = None
    if term:
        # Dedicated lookup by account number / customer ID / loan number
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        found = branch_scope(Loan.query, Loan).filter(
            Loan.loan_type == "gold", db.or_(Loan.customer_id.in_(ids), Loan.loan_number == term.upper())).all()
    return _loan_list("gold", template="loans/gold.html", gold_weight=gold_stats[0],
                      gold_count=gold_stats[1], found=found)


@bp.route("/<int:lid>")
@login_required
@require_perm("loans", "view")
def detail(lid):
    loan = ensure_branch_access(db.session.get(Loan, lid))
    approvals = ApprovalRequest.query.filter_by(ref_type="loan", ref_id=loan.id).order_by(ApprovalRequest.created_at).all()
    repayments = loan.repayments.all()
    # Projected schedule for the next 6 instalments
    schedule = []
    if loan.status in ("Active", "Overdue") and loan.next_due_date:
        bal = loan.outstanding
        due = loan.next_due_date
        for _ in range(6):
            if bal <= 0:
                break
            interest = round(bal * loan.interest_rate / 1200, 2)
            principal = round(min(loan.emi - interest, bal), 2)
            bal = round(bal - principal, 2)
            schedule.append({"due": due, "emi": round(principal + interest, 2), "principal": principal,
                             "interest": interest, "balance": max(bal, 0)})
            due = add_months(due, 1)
    audit("Loan viewed", "loans", loan.loan_number)
    db.session.commit()
    return render_template("loans/detail.html", loan=loan, approvals=approvals, repayments=repayments,
                           schedule=schedule, documents=loan.documents.all())


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("loans", "create")
def new():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        f = request.form
        ltype = f.get("loan_type")
        amount = parse_float(f.get("amount"), 0)
        rate = parse_float(f.get("interest_rate"), 0)
        months = f.get("tenure_months", type=int) or 0
        errors = []
        if ltype not in LOAN_TYPE_LABELS:
            errors.append("Select a loan type.")
        if amount <= 0 or rate <= 0 or months <= 0:
            errors.append("Amount, interest rate and tenure must be positive.")
        if customer.kyc_status != "Verified":
            errors.append("Customer KYC must be verified before a loan can be created.")
        account_id = f.get("account_id", type=int)
        acct = db.session.get(Account, account_id) if account_id else None
        if acct and acct.customer_id != customer.id:
            errors.append("Disbursement account must belong to the customer.")
        if ltype == "gold" and not parse_float(f.get("gold_weight_grams")):
            errors.append("Gold weight is required for gold loans.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("loans/new.html", customer=customer, f=f)
        loan = Loan(loan_number=unique_number(Loan, "loan_number", "LN" + ltype[:2].upper(), 8),
                    customer_id=customer.id, account_id=acct.id if acct else None, branch_id=customer.branch_id,
                    loan_type=ltype, amount=amount, outstanding=0, interest_rate=rate, tenure_months=months,
                    emi=calc_emi(amount, rate, months), status="Pending", purpose=f.get("purpose"),
                    collateral=f.get("collateral"), created_by=current_user.id)
        if ltype == "gold":
            loan.gold_weight_grams = parse_float(f.get("gold_weight_grams"))
            loan.gold_purity = f.get("gold_purity")
            loan.gold_items = f.get("gold_items")
            loan.gold_valuation = parse_float(f.get("gold_valuation"))
        db.session.add(loan)
        db.session.flush()
        activity(customer.id, "Loan created", loan.loan_number, f"{loan.type_label} ₹{amount:,.0f} (pending approval)")
        req = create_approval("loan_sanction", "loan", loan.id, customer, amount,
                              f"{loan.type_label} of ₹{amount:,.0f} for {customer.name}")
        audit("Loan created", "loans", loan.loan_number)
        db.session.commit()
        flash(f"Loan {loan.loan_number} created and sent for approval ({req.request_number}).", "success")
        return redirect(url_for("loans.detail", lid=loan.id))
    return render_template("loans/new.html", customer=customer, f=request.args)


@bp.route("/<int:lid>/repay", methods=["POST"])
@login_required
@require_perm("loans", "edit")
def repay(lid):
    loan = ensure_branch_access(db.session.get(Loan, lid))
    amount = parse_float(request.form.get("amount"), 0)
    mode = request.form.get("mode", "Cash")
    if loan.status not in ("Active", "Overdue") or amount <= 0:
        flash("Repayment can only be posted on an active loan with a positive amount.", "danger")
        return redirect(url_for("loans.detail", lid=lid))
    interest = round(min(loan.outstanding * loan.interest_rate / 1200, amount), 2)
    principal = round(min(amount - interest, loan.outstanding), 2)
    today = date.today()
    late = bool(loan.next_due_date and today > loan.next_due_date)
    db.session.add(LoanRepayment(loan_id=loan.id, due_date=loan.next_due_date, paid_on=today, amount=amount,
                                 principal_component=principal, interest_component=interest, mode=mode,
                                 status="Late" if late else "Paid"))
    loan.outstanding = round(loan.outstanding - principal, 2)
    if mode == "Account debit" and loan.account:
        try:
            post_transaction(loan.account, "Debit", amount, "Transfer", "Loan EMI", f"EMI for {loan.loan_number}")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("loans.detail", lid=lid))
    if loan.outstanding <= 0:
        loan.outstanding = 0
        loan.status = "Closed"
        loan.next_due_date = None
        activity(loan.customer_id, "Loan closed", loan.loan_number)
    elif amount >= loan.emi * 0.99:
        loan.next_due_date = add_months(loan.next_due_date or today, 1)
        if loan.status == "Overdue" and loan.next_due_date >= today:
            loan.status = "Active"
            loan.overdue_days = 0
    activity(loan.customer_id, "Payment received", loan.loan_number, f"₹{amount:,.0f} via {mode}")
    audit("Loan repayment posted", "loans", loan.loan_number, f"₹{amount:,.2f} {mode}")
    db.session.commit()
    flash(f"Repayment of ₹{amount:,.0f} posted.", "success")
    return redirect(url_for("loans.detail", lid=lid))
