"""Business services.

In production these functions are the integration seam with the core banking
system: they would call authenticated core-banking APIs instead of writing to
the local database directly. Views never mutate financial records themselves;
they go through these services (and the maker-checker workflow) instead.
"""
import json
from datetime import date, datetime, timedelta

from flask import abort
from flask_login import current_user
from sqlalchemy import func, or_

from .extensions import db
from .models import (Account, Alert, AlertRule, ApprovalRequest, Customer, Document,
                     FixedDeposit, Loan, Setting, Transaction)
from .security import activity, audit
from .utils import add_months, unique_number

FINAL_APPROVER_LEVEL = 80  # Branch Manager and above


# --------------------------------------------------------------------------
# Maker-checker workflow
# --------------------------------------------------------------------------

APPROVAL_MODULE = {
    "loan_sanction": "loan_approval",
    "account_open": "customer_accounts",
    "account_close": "customer_accounts",
    "fd_create": "deposits",
}


def create_approval(request_type, ref_type, ref_id, customer, amount, summary, payload=None):
    req = ApprovalRequest(
        request_number=unique_number(ApprovalRequest, "request_number", "APR", 8),
        request_type=request_type, module=APPROVAL_MODULE[request_type],
        ref_type=ref_type, ref_id=ref_id, customer_id=customer.id,
        branch_id=customer.branch_id, amount=amount, summary=summary,
        payload_json=json.dumps(payload or {}), maker_id=current_user.id,
    )
    db.session.add(req)
    db.session.flush()
    audit("Approval request created", req.module, req.request_number, summary)
    activity(customer.id, "Approval requested", req.request_number, summary)
    raise_alert("pending_approval", f"{req.type_label} awaiting review",
                f"{summary} — raised by {current_user.name}", module=req.module,
                reference=req.request_number, link=f"/approvals/{req.id}",
                customer_id=customer.id, branch_id=customer.branch_id, severity="Medium")
    return req


def can_review(req, user):
    return (req.status == "Pending Review" and user.can(req.module, "approve")
            and user.id != req.maker_id)


def can_decide(req, user):
    return (req.status == "Reviewed" and user.can(req.module, "approve")
            and user.role.level >= FINAL_APPROVER_LEVEL
            and user.id not in (req.maker_id, req.checker_id))


def can_reject(req, user):
    if req.status not in ("Pending Review", "Reviewed") or not user.can(req.module, "reject"):
        return False
    return user.id != req.maker_id and (req.status == "Pending Review" or can_decide(req, user))


def review_approval(req, remarks):
    if not can_review(req, current_user):
        abort(403)
    req.status = "Reviewed"
    req.checker_id = current_user.id
    req.checker_remarks = remarks
    req.checked_at = datetime.utcnow()
    audit("Approval reviewed (checker)", req.module, req.request_number, remarks)
    activity(req.customer_id, "Approval reviewed", req.request_number, remarks)


def approve_approval(req, remarks):
    if not can_decide(req, current_user):
        abort(403)
    req.status = "Approved"
    req.approver_id = current_user.id
    req.approver_remarks = remarks
    req.decided_at = datetime.utcnow()
    _execute(req, approved=True)
    audit("Approval approved", req.module, req.request_number, remarks)
    activity(req.customer_id, "Approval completed", req.request_number,
             f"{req.type_label} approved by {current_user.name}")
    _resolve_pending_alert(req)


def reject_approval(req, remarks):
    if not can_reject(req, current_user):
        abort(403)
    req.status = "Rejected"
    if req.checker_id is None:
        req.checker_id = current_user.id
        req.checked_at = datetime.utcnow()
        req.checker_remarks = remarks
    req.approver_id = current_user.id
    req.approver_remarks = remarks
    req.decided_at = datetime.utcnow()
    _execute(req, approved=False)
    audit("Approval rejected", req.module, req.request_number, remarks)
    activity(req.customer_id, "Approval rejected", req.request_number, remarks)
    _resolve_pending_alert(req)


def _resolve_pending_alert(req):
    Alert.query.filter_by(alert_type="pending_approval", reference=req.request_number,
                          is_resolved=False).update(
        {"is_resolved": True, "resolved_by": current_user.id, "resolved_at": datetime.utcnow()})


def _execute(req, approved):
    today = date.today()
    if req.request_type == "loan_sanction":
        loan = db.session.get(Loan, req.ref_id)
        if approved:
            loan.status = "Active"
            loan.disbursed_on = today
            loan.outstanding = loan.amount
            loan.next_due_date = add_months(today, 1)
            if loan.account:
                post_transaction(loan.account, "Credit", loan.amount, "Transfer",
                                 "Loan Disbursement", f"Disbursal of {loan.loan_number}")
            activity(loan.customer_id, "Loan disbursed", loan.loan_number)
        else:
            loan.status = "Rejected"
    elif req.request_type == "account_open":
        acc = db.session.get(Account, req.ref_id)
        acc.status = "Active" if approved else "Closed"
        if approved:
            activity(acc.customer_id, "Account created", acc.account_number,
                     f"{acc.account_type} account activated")
    elif req.request_type == "account_close" and approved:
        acc = db.session.get(Account, req.ref_id)
        acc.status = "Closed"
        acc.closed_on = today
        activity(acc.customer_id, "Account closed", acc.account_number)
    elif req.request_type == "fd_create":
        fd = db.session.get(FixedDeposit, req.ref_id)
        fd.status = "Active" if approved else "Closed"
        if approved:
            activity(fd.customer_id, "FD created", fd.fd_number)
        else:
            funding = db.session.get(Account, req.payload.get("funding_account_id") or 0)
            if funding and funding.status != "Closed":
                post_transaction(funding, "Credit", fd.principal, "Transfer", "FD Reversal",
                                 f"Refund for rejected FD {fd.fd_number}")


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

def post_transaction(account, txn_type, amount, channel, category, description, when=None):
    if account.status in ("Closed", "Pending"):
        raise ValueError("Transactions are not allowed on a closed or pending account.")
    if txn_type == "Debit" and amount > account.balance:
        raise ValueError("Insufficient balance for this debit.")
    account.balance += amount if txn_type == "Credit" else -amount
    account.last_txn_on = (when or datetime.utcnow()).date()
    if account.status in ("Dormant", "Inactive"):
        prior_status = account.status
        account.status = "Active"
    else:
        prior_status = None
    txn = Transaction(
        txn_ref=unique_number(Transaction, "txn_ref", "TXN", 12), account_id=account.id,
        customer_id=account.customer_id, branch_id=account.branch_id, txn_type=txn_type,
        channel=channel, category=category, amount=amount, balance_after=account.balance,
        description=description, timestamp=when or datetime.utcnow(),
        created_by=current_user.id if current_user and current_user.is_authenticated else None,
    )
    db.session.add(txn)
    db.session.flush()
    evaluate_transaction(txn, prior_status)
    return txn


# --------------------------------------------------------------------------
# Alerts & risk rules
# --------------------------------------------------------------------------

def raise_alert(alert_type, title, message, module="alerts", severity="Medium", reference=None,
                link=None, customer_id=None, branch_id=None, dedupe=True):
    if dedupe and reference:
        exists = Alert.query.filter_by(alert_type=alert_type, reference=reference,
                                       is_resolved=False).first()
        if exists:
            return exists
    alert = Alert(alert_type=alert_type, title=title, message=message, module=module,
                  severity=severity, reference=reference, link=link,
                  customer_id=customer_id, branch_id=branch_id)
    db.session.add(alert)
    return alert


def evaluate_transaction(txn, prior_status=None):
    for rule in AlertRule.query.filter_by(enabled=True).all():
        if rule.rule_type == "high_value_txn" and txn.amount >= rule.threshold:
            txn.flagged = True
            raise_alert("fraud_risk", f"High-value transaction {txn.txn_ref}",
                        f"{txn.txn_type} of ₹{txn.amount:,.0f} via {txn.channel} exceeds the "
                        f"₹{rule.threshold:,.0f} threshold ({rule.name}).",
                        module=rule.route_module, severity=rule.severity, reference=txn.txn_ref,
                        link=f"/customers/{txn.customer_id}?tab=transactions",
                        customer_id=txn.customer_id, branch_id=txn.branch_id)
        elif rule.rule_type == "txn_velocity":
            since = txn.timestamp - timedelta(minutes=rule.window_minutes)
            count = Transaction.query.filter(Transaction.account_id == txn.account_id,
                                             Transaction.timestamp >= since).count()
            if count >= rule.threshold:
                txn.flagged = True
                raise_alert("unusual_txn", f"Rapid transactions on account",
                            f"{count} transactions within {rule.window_minutes} min on account "
                            f"{txn.account.account_number}.", module=rule.route_module,
                            severity=rule.severity, reference=f"VEL-{txn.account_id}",
                            link=f"/customers/{txn.customer_id}?tab=transactions",
                            customer_id=txn.customer_id, branch_id=txn.branch_id)
        elif rule.rule_type == "dormant_activity" and prior_status == "Dormant":
            txn.flagged = True
            raise_alert("unusual_txn", "Activity on dormant account",
                        f"Dormant account {txn.account.account_number} received a "
                        f"{txn.txn_type.lower()} of ₹{txn.amount:,.0f}.", module=rule.route_module,
                        severity=rule.severity, reference=txn.txn_ref,
                        link=f"/customers/{txn.customer_id}?tab=transactions",
                        customer_id=txn.customer_id, branch_id=txn.branch_id)


def failed_login_alert(email, attempts, employee=None):
    rule = AlertRule.query.filter_by(rule_type="failed_logins", enabled=True).first()
    threshold = int(rule.threshold) if rule else 3
    if attempts >= threshold:
        raise_alert("failed_login", f"Multiple failed logins for {email}",
                    f"{attempts} consecutive failed sign-in attempts.", module="user_management",
                    severity=rule.severity if rule else "High",
                    reference=f"LOGIN-{email}-{date.today().isoformat()}",
                    link="/users/login-history",
                    branch_id=employee.branch_id if employee else None)


def scan_scheduled_alerts(force=False):
    """EMI due, overdue loans, KYC/document expiry. Runs at most hourly."""
    last = Setting.get("last_alert_scan")
    if not force and last:
        try:
            if datetime.utcnow() - datetime.fromisoformat(last) < timedelta(hours=1):
                return
        except ValueError:
            pass
    today = date.today()
    emi_days = Setting.get_int("emi_due_warning_days")
    kyc_days = Setting.get_int("kyc_expiry_warning_days")

    for loan in Loan.query.filter(Loan.status == "Active", Loan.next_due_date != None,  # noqa
                                  Loan.next_due_date <= today + timedelta(days=emi_days)):
        if loan.next_due_date < today:
            loan.status = "Overdue"
            loan.overdue_days = (today - loan.next_due_date).days
        else:
            raise_alert("emi_due", f"EMI due for {loan.loan_number}",
                        f"{loan.customer.name}: EMI ₹{loan.emi:,.0f} due on "
                        f"{loan.next_due_date:%d %b %Y}.", module="loans", severity="Low",
                        reference=f"EMI-{loan.loan_number}-{loan.next_due_date}",
                        link=f"/loans/{loan.id}", customer_id=loan.customer_id,
                        branch_id=loan.branch_id)
    for loan in Loan.query.filter(Loan.status == "Overdue"):
        if loan.next_due_date:
            loan.overdue_days = max((today - loan.next_due_date).days, 0)
        raise_alert("overdue_loan", f"Overdue loan {loan.loan_number}",
                    f"{loan.customer.name}: {loan.overdue_days} days overdue, outstanding "
                    f"₹{loan.outstanding:,.0f}.", module="loans",
                    severity="High" if loan.overdue_days > 60 else "Medium",
                    reference=f"OD-{loan.loan_number}", link=f"/loans/{loan.id}",
                    customer_id=loan.customer_id, branch_id=loan.branch_id)
    for doc in Document.query.filter(Document.expiry_date != None,  # noqa
                                     Document.expiry_date <= today + timedelta(days=kyc_days),
                                     Document.status.in_(["Verified", "Pending"])):
        expired = doc.expiry_date < today
        if expired:
            doc.status = "Expired"
            if doc.category == "KYC":
                doc.customer.kyc_status = "Expired"
        kind = "kyc_expiry" if doc.category == "KYC" else "document_expiry"
        raise_alert(kind, f"{doc.doc_type} {'expired' if expired else 'expiring'}",
                    f"{doc.customer.name}: {doc.doc_type} {'expired' if expired else 'expires'} "
                    f"on {doc.expiry_date:%d %b %Y}.", module="kyc",
                    severity="High" if expired else "Medium",
                    reference=f"DOC-{doc.id}-{doc.expiry_date}",
                    link=f"/customers/{doc.customer_id}?tab=documents",
                    customer_id=doc.customer_id, branch_id=doc.customer.branch_id)
    for fd in FixedDeposit.query.filter(FixedDeposit.status == "Active",
                                        FixedDeposit.maturity_date < today):
        fd.status = "Matured"
    # Accounts with no activity for 12+ months become dormant, 6+ months inactive.
    Account.query.filter(Account.status == "Active",
                         Account.last_txn_on < today - timedelta(days=365)).update(
        {"status": "Dormant"}, synchronize_session=False)
    Account.query.filter(Account.status == "Active",
                         Account.last_txn_on < today - timedelta(days=180)).update(
        {"status": "Inactive"}, synchronize_session=False)
    Setting.set("last_alert_scan", datetime.utcnow().isoformat())
    db.session.commit()


def severity_order():
    return db.case({"Critical": 0, "High": 1, "Medium": 2}, value=Alert.severity, else_=3)


def visible_alerts(user):
    """Alerts are routed to employees who can view the alert's module, scoped by branch."""
    from .models import MODULES
    allowed = [m for m, _ in MODULES if user.can(m, "view")]
    q = Alert.query.filter(Alert.module.in_(allowed))
    if not user.sees_all_branches:
        q = q.filter(or_(Alert.branch_id == user.branch_id, Alert.branch_id == None))  # noqa
    return q


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def search_customers(term, base_query):
    from .crypto import search_hash
    term = (term or "").strip()
    if not term:
        return base_query.filter(False)
    h = search_hash(term)
    like = f"%{term}%"
    acc_cust = db.session.query(Account.customer_id).filter(Account.account_number == term)
    loan_cust = db.session.query(Loan.customer_id).filter(Loan.loan_number == term.upper())
    return base_query.filter(or_(
        Customer.customer_code == term.upper(), Customer.name.ilike(like),
        Customer.pan_hash == h, Customer.mobile_hash == h,
        Customer.id.in_(acc_cust), Customer.id.in_(loan_cust),
    ))


def sum_or_zero(q):
    return q.scalar() or 0


def money_sum(column, *filters):
    return sum_or_zero(db.session.query(func.coalesce(func.sum(column), 0)).filter(*filters))
