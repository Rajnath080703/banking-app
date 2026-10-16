from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import LOAN_TYPE_LABELS, Account, ApprovalRequest, Customer, Loan, LoanRepayment
from ..money import D, ZERO, parse_money, parse_rate, q2
from ..private_search import term as search_term
from ..security import activity, audit, branch_scope, customer_from_request, ensure_branch_access, once_per_form, require_perm
from ..services import (RepaymentError, create_approval, loan_dues, money_sum, pending_request, repay_loan,
                        search_customers)
from ..utils import add_months, calc_emi, local_today, unique_number, inr
from ._listing import apply_sort

bp = Blueprint("loans", __name__, url_prefix="/loans")

FILTERS = [("all", "All Loans"), ("Active", "Active"), ("Closed", "Closed"),
           ("Overdue", "Overdue"), ("Pending", "Pending Approval"), ("Rejected", "Rejected")]
MIN_LOAN, MAX_LOAN = Decimal("1000"), Decimal("1000000000")   # ₹1,000 – ₹100 crore
MAX_RATE, MAX_TENURE = Decimal("36"), 360
GOLD_MAX_LTV = Decimal("0.75")                                 # RBI cap on gold-loan loan-to-value
SORTS = {"created": Loan.created_at, "amount": Loan.amount, "outstanding": Loan.outstanding,
         "due": Loan.next_due_date, "number": Loan.loan_number}


def _loan_list(loan_type=None, template="loans/index.html", **extra):
    q = branch_scope(Loan.query, Loan)
    if loan_type:
        q = q.filter(Loan.loan_type == loan_type)
    status = request.args.get("status", "all")
    if status != "all":
        q = q.filter(Loan.status == status)
    term = search_term()
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(Loan.customer_id.in_(ids), Loan.loan_number == term.upper()))
    q, sort = apply_sort(q, SORTS, "created")
    page = request.args.get("page", 1, type=int)
    loans = q.paginate(page=page, per_page=25, error_out=False)

    base = branch_scope(Loan.query, Loan)
    if loan_type:
        base = base.filter(Loan.loan_type == loan_type)
    counts = dict(base.with_entities(Loan.status, func.count()).group_by(Loan.status).all())
    counts["all"] = sum(counts.values())
    ids_q = base.with_entities(Loan.id)
    stats = {
        "sanctioned": q2(base.filter(Loan.disbursed_on != None).with_entities(func.coalesce(func.sum(Loan.amount), 0)).scalar()),  # noqa: E711
        "outstanding": q2(base.filter(Loan.status.in_(["Active", "Overdue"])).with_entities(func.coalesce(func.sum(Loan.outstanding), 0)).scalar()),
        "overdue": q2(base.filter(Loan.status == "Overdue").with_entities(func.coalesce(func.sum(Loan.outstanding), 0)).scalar()),
        "collected_month": money_sum(LoanRepayment.amount, LoanRepayment.loan_id.in_(ids_q),
                                     LoanRepayment.paid_on >= local_today().replace(day=1)),
    }
    return render_template(template, loans=loans, status=status, term=term, counts=counts, sort=sort,
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
    term = search_term()
    gold_stats = branch_scope(Loan.query, Loan).filter(
        Loan.loan_type == "gold", Loan.status.in_(["Active", "Overdue"])).with_entities(
        func.coalesce(func.sum(Loan.gold_weight_grams), 0), func.count()).one()
    found = None
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        found = branch_scope(Loan.query, Loan).filter(
            Loan.loan_type == "gold", db.or_(Loan.customer_id.in_(ids), Loan.loan_number == term.upper())).limit(20).all()
    return _loan_list("gold", template="loans/gold.html", gold_weight=gold_stats[0],
                      gold_count=gold_stats[1], found=found)


def projected_schedule(loan, count=6):
    """Next instalments on the reducing balance (monthly interest = balance × rate / 1200)."""
    rows = []
    if loan.status not in ("Active", "Overdue") or not loan.next_due_date:
        return rows
    bal, due = q2(loan.outstanding), loan.next_due_date
    for _ in range(count):
        if bal <= ZERO:
            break
        interest = q2(bal * D(loan.interest_rate) / 1200)
        principal = min(q2(loan.emi) - interest, bal)
        if principal <= ZERO:  # EMI does not even cover interest (rate raised): show interest-only
            principal = ZERO
        bal = q2(bal - principal)
        rows.append({"due": due, "emi": q2(principal + interest), "principal": principal, "interest": interest,
                     "balance": bal})
        due = add_months(due, 1)
    return rows


@bp.route("/<int:lid>")
@login_required
@require_perm("loans", "view")
def detail(lid):
    loan = ensure_branch_access(db.session.get(Loan, lid))
    approvals = ApprovalRequest.query.filter_by(ref_type="loan", ref_id=loan.id).order_by(ApprovalRequest.created_at).all()
    repayments = loan.repayments.all()
    dues = loan_dues(loan) if loan.status in ("Active", "Overdue") else None
    paid_total = q2(sum((r.amount for r in repayments), ZERO))
    audit("Loan viewed", "loans", loan.loan_number)
    db.session.commit()
    return render_template("loans/detail.html", loan=loan, approvals=approvals, repayments=repayments,
                           schedule=projected_schedule(loan), documents=loan.documents.all(), dues=dues,
                           paid_total=paid_total, rate_change_pending=pending_request("loan_rate_change", loan.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("loans", "create")
@once_per_form
def new():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        f = request.form
        ltype = f.get("loan_type")
        amount = parse_money(f.get("amount"), MIN_LOAN, MAX_LOAN)
        rate = parse_rate(f.get("interest_rate"), MAX_RATE)
        months = f.get("tenure_months", type=int) or 0
        errors = []
        if ltype not in LOAN_TYPE_LABELS:
            errors.append("Select a loan type.")
        if amount is None:
            errors.append("Loan amount must be between ₹1,000 and ₹100 crore, with at most two decimals.")
        if rate is None:
            errors.append(f"Interest rate must be above 0% and at most {MAX_RATE}% a year.")
        if not 1 <= months <= MAX_TENURE:
            errors.append(f"Tenure must be between 1 and {MAX_TENURE} months.")
        if customer.kyc_status != "Verified":
            errors.append("Customer KYC must be verified before a loan can be created.")
        account_id = f.get("account_id", type=int)
        acct = db.session.get(Account, account_id) if account_id else None
        if acct and acct.customer_id != customer.id:
            errors.append("Disbursement account must belong to the customer.")
        elif acct and acct.status not in ("Active", "Inactive", "Dormant"):
            errors.append("The disbursement account is not open.")
        grams = valuation = None
        if ltype == "gold":
            grams = parse_rate(f.get("gold_weight_grams"), Decimal("100000"))
            valuation = parse_money(f.get("gold_valuation"))
            if grams is None:
                errors.append("Gold weight (grams) is required for gold loans.")
            if valuation is None:
                errors.append("Gold valuation is required for gold loans.")
            elif amount and amount > q2(valuation * GOLD_MAX_LTV):
                errors.append(f"Gold loan exceeds the {GOLD_MAX_LTV:.0%} loan-to-value limit: the maximum against "
                              f"{inr(valuation)} of gold is {inr(q2(valuation * GOLD_MAX_LTV))}.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("loans/new.html", customer=customer, f=f)
        loan = Loan(loan_number=unique_number(Loan, "loan_number", "LN" + ltype[:2].upper(), 8),
                    customer_id=customer.id, account_id=acct.id if acct else None, branch_id=customer.branch_id,
                    loan_type=ltype, amount=amount, outstanding=ZERO, interest_rate=rate, tenure_months=months,
                    emi=calc_emi(amount, rate, months), status="Pending", purpose=(f.get("purpose") or "")[:255],
                    collateral=(f.get("collateral") or "")[:255], created_by=current_user.id)
        if ltype == "gold":
            loan.gold_weight_grams = grams
            loan.gold_purity = f.get("gold_purity") if f.get("gold_purity") in ("18K", "22K", "24K") else None
            loan.gold_items = (f.get("gold_items") or "")[:255]
            loan.gold_valuation = valuation
        db.session.add(loan)
        db.session.flush()
        activity(customer.id, "Loan created", loan.loan_number, f"{loan.type_label} {inr(amount, 2)} (pending approval)")
        req = create_approval("loan_sanction", "loan", loan.id, customer, amount,
                              f"{loan.type_label} of {inr(amount, 2)} for {customer.name}")
        audit("Loan created", "loans", loan.loan_number)
        db.session.commit()
        flash(f"Loan {loan.loan_number} created and sent for approval ({req.request_number}).", "success")
        return redirect(url_for("loans.detail", lid=loan.id))
    return render_template("loans/new.html", customer=customer, f=request.args)


@bp.route("/<int:lid>/repay", methods=["POST"])
@login_required
@require_perm("loans", "edit")
@once_per_form
def repay(lid):
    loan = ensure_branch_access(db.session.get(Loan, lid))
    amount = parse_money(request.form.get("amount"))
    mode = request.form.get("mode", "Cash")
    if mode not in ("Cash", "NEFT", "UPI", "Cheque", "Account debit"):
        mode = "Cash"
    if amount is None:
        flash("Enter a repayment amount of at least ₹0.01, with at most two decimals.", "danger")
        return redirect(url_for("loans.detail", lid=lid))
    try:
        r = repay_loan(loan, amount, mode)
    except (RepaymentError, ValueError) as exc:
        db.session.rollback()
        audit("Loan repayment failed", "loans", loan.loan_number, f"{inr(amount, 2)} {mode}: {exc}", result="failed")
        db.session.commit()
        flash(str(exc), "danger")
        return redirect(url_for("loans.detail", lid=lid))
    audit("Loan repayment posted", "loans", loan.loan_number, f"{inr(amount, 2)} {mode}")
    db.session.commit()
    flash(f"Repayment of {inr(r.amount, 2)} posted: penal {inr(r.penalty, 2)}, interest {inr(r.interest_component, 2)}, "
          f"principal {inr(r.principal_component, 2)}.", "success")
    return redirect(url_for("loans.detail", lid=lid))


@bp.route("/<int:lid>/rate", methods=["POST"])
@login_required
@require_perm("loans", "edit")
@once_per_form
def rate_change(lid):
    """Interest-rate change is a maker-checker request; nothing changes until approved."""
    loan = ensure_branch_access(db.session.get(Loan, lid))
    new_rate = parse_rate(request.form.get("new_rate"), MAX_RATE)
    reason = (request.form.get("reason") or "").strip()[:200]
    if loan.status not in ("Active", "Overdue"):
        flash("Only a running loan's rate can be changed.", "danger")
    elif new_rate is None or new_rate == D(loan.interest_rate):
        flash(f"Enter a new rate above 0% and at most {MAX_RATE}%, different from the current one.", "danger")
    elif not reason:
        flash("Give a reason for the rate change.", "danger")
    elif pending_request("loan_rate_change", loan.id):
        flash("A rate change for this loan is already waiting for approval.", "warning")
    else:
        req = create_approval("loan_rate_change", "loan", loan.id, loan.customer, None,
                              f"Rate change on {loan.loan_number}: {loan.interest_rate}% → {new_rate}% — {reason}",
                              {"new_rate": str(new_rate), "old_rate": str(loan.interest_rate), "reason": reason})
        db.session.commit()
        flash(f"Rate change sent for maker-checker approval ({req.request_number}).", "success")
    return redirect(url_for("loans.detail", lid=lid))
