"""Business services — the only code that moves money or changes regulated records.

Views validate input and check permissions; everything that changes balances, loans,
deposits or approvals goes through here, inside the caller's database transaction:
  * rows whose balances change are locked (SELECT ... FOR UPDATE),
  * every money movement is posted to the double-entry ledger (ledger.py),
  * all arithmetic is Decimal with half-up rounding to the paisa (money.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from flask import abort, has_request_context
from flask_login import current_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from . import ledger as L
from .extensions import db
from .models import (Account, Alert, AlertRule, ApprovalRequest, Customer, Document, FixedDeposit,
                     Loan, LoanRepayment, Permission, Role, Setting, Transaction)
from .money import D, ZERO, allocate_payment, emi as calc_emi, fd_maturity, q2, simple_interest
from .security import activity, audit
from .utils import add_months, local_today, unique_number, inr

FINAL_APPROVER_LEVEL = 80        # Branch Manager and above
APPROVAL_MODULE = {
    "loan_sanction": "loan_approval",
    "loan_rate_change": "loan_approval",
    "account_open": "customer_accounts",
    "account_close": "customer_accounts",
    "account_freeze": "customer_accounts",
    "account_unfreeze": "customer_accounts",
    "customer_update": "customer_profile",
    "fd_create": "deposits",
    "fd_close": "deposits",
    "large_txn": "transactions",
    "txn_reversal": "transactions",
    "fund_transfer": "transactions",
    "interest_posting": "interest",
    "role_permissions": "user_management",
    "fee_waiver": "transactions",
}
# Some decisions need a more senior final approver than the default.
FINAL_LEVEL_BY_TYPE = {"role_permissions": 90, "interest_posting": 90}


class ApprovalBlocked(Exception):
    """The approved action can no longer be carried out (e.g. the balance changed meanwhile)."""


def _actor():
    if has_request_context() and current_user and current_user.is_authenticated:
        return current_user
    return None


def _actor_id():
    a = _actor()
    return a.id if a else None


def lock(obj):
    """Re-read a row with SELECT ... FOR UPDATE so concurrent requests queue instead of
    overwriting each other's changes."""
    db.session.refresh(obj, with_for_update=True)
    return obj


# --------------------------------------------------------------------------
# Maker-checker workflow
# --------------------------------------------------------------------------

def create_approval(request_type, ref_type, ref_id, customer, amount, summary, payload=None, branch_id=None):
    req = ApprovalRequest(
        request_number=unique_number(ApprovalRequest, "request_number", "APR", 8),
        request_type=request_type, module=APPROVAL_MODULE[request_type],
        ref_type=ref_type, ref_id=ref_id, customer_id=customer.id if customer else None,
        branch_id=customer.branch_id if customer else (branch_id or current_user.branch_id),
        amount=q2(amount) if amount is not None else None, summary=summary[:255],
        maker_id=current_user.id,  # the maker is always the signed-in employee
    )
    req.payload = payload or {}
    db.session.add(req)
    db.session.flush()
    audit("Approval request created", req.module, req.request_number, summary)
    if customer:
        activity(customer.id, "Approval requested", req.request_number, summary)
    raise_alert("pending_approval", f"{req.type_label} awaiting review",
                f"{summary} — raised by {current_user.name}", module=req.module,
                reference=req.request_number, link=f"/approvals/{req.id}",
                customer_id=req.customer_id, branch_id=req.branch_id, severity="Medium")
    return req


def pending_request(request_type, ref_id):
    return ApprovalRequest.query.filter(ApprovalRequest.request_type == request_type,
                                        ApprovalRequest.ref_id == ref_id,
                                        ApprovalRequest.status.in_(["Pending Review", "Reviewed"])).first()


def can_review(req, user):
    return (req.status == "Pending Review" and user.can(req.module, "approve")
            and user.id != req.maker_id)


def can_decide(req, user):
    level = FINAL_LEVEL_BY_TYPE.get(req.request_type, FINAL_APPROVER_LEVEL)
    return (req.status == "Reviewed" and user.can(req.module, "approve")
            and user.role.level >= level
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
    if req.customer_id:
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
    if req.customer_id:
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
    if req.customer_id:
        activity(req.customer_id, "Approval rejected", req.request_number, remarks)
    _resolve_pending_alert(req)


def _resolve_pending_alert(req):
    Alert.query.filter_by(alert_type="pending_approval", reference=req.request_number,
                          is_resolved=False).update(
        {"is_resolved": True, "resolved_by": current_user.id, "resolved_at": datetime.utcnow()})


def _execute(req, approved):
    handler = _HANDLERS.get(req.request_type)
    if handler:
        handler(req, approved)


def _exec_large_txn(req, approved):
    if not approved:
        return
    acc = db.session.get(Account, req.ref_id)
    p = req.payload
    try:
        txn = post_transaction(acc, p["txn_type"], req.amount, p.get("channel", "Cash"),
                               "Deposit" if p["txn_type"] == "Credit" else "Withdrawal",
                               p.get("description") or "Approved high-value transaction")
    except ValueError as exc:
        raise ApprovalBlocked(str(exc)) from exc
    activity(acc.customer_id, "High-value transaction posted", txn.txn_ref, f"{inr(req.amount, 2)} after approval")


# Only teller postings are reversed here. Loan, FD, interest and EMI postings have their own
# correction processes; reversing them directly would leave the loan or FD record out of step.
REVERSIBLE_CATEGORIES = ("Deposit", "Withdrawal")


def reversal_problem(txn):
    """Why this transaction cannot be reversed now, or None. Checked when the request is raised
    and again, under a row lock, when it is approved."""
    if txn.status == "Reversed":
        return f"Transaction {txn.txn_ref} has already been reversed."
    if txn.category not in REVERSIBLE_CATEGORIES:
        return (f"{txn.category or 'This'} postings cannot be reversed here; they are corrected through "
                f"their own loan, deposit or interest process.")
    acc = txn.account
    if acc.status in ("Closed", "Pending", "Rejected"):
        return f"Account {acc.account_number} is {acc.status.lower()}."
    if txn.txn_type == "Credit":  # undoing a deposit takes the money back out
        if acc.status == "Frozen":
            return f"Account {acc.account_number} is frozen, so the deposit cannot be taken back out. Unfreeze it first."
        if q2(acc.balance) < txn.amount:
            return (f"Reversing this deposit needs {inr(txn.amount, 2)} but account {acc.account_number} "
                    f"holds {inr(acc.balance, 2)}.")
    return None


def _exec_txn_reversal(req, approved):
    if not approved:
        return
    txn = lock(db.session.get(Transaction, req.ref_id))  # a second approval of a duplicate waits here
    lock(txn.account)
    problem = reversal_problem(txn)
    if problem:
        raise ApprovalBlocked(problem)
    reason = (req.payload or {}).get("reason", "")
    try:
        rev = post_transaction(txn.account, "Debit" if txn.txn_type == "Credit" else "Credit", txn.amount, txn.channel,
                               "Reversal", f"Reversal of {txn.txn_ref}: {reason}"[:255])
    except ValueError as exc:
        raise ApprovalBlocked(str(exc)) from exc
    txn.status = "Reversed"
    activity(txn.customer_id, "Transaction reversed", txn.txn_ref, f"{inr(txn.amount, 2)} reversed by {rev.txn_ref}")


def _exec_loan_sanction(req, approved):
    loan = lock(db.session.get(Loan, req.ref_id))
    if not approved:
        loan.status = "Rejected"
        return
    disburse_loan(loan)


def _exec_loan_rate_change(req, approved):
    if not approved:
        return
    loan = lock(db.session.get(Loan, req.ref_id))
    if loan.status not in ("Active", "Overdue"):
        raise ApprovalBlocked("The loan is no longer running.")
    old_rate, old_emi = loan.interest_rate, loan.emi
    new_rate = D(req.payload["new_rate"])
    loan.interest_rate = new_rate
    loan.emi = calc_emi(loan.outstanding, new_rate, remaining_instalments(loan))
    activity(loan.customer_id, "Loan interest rate changed", loan.loan_number,
             f"{old_rate}% → {new_rate}%; EMI {inr(old_emi, 2)} → {inr(loan.emi, 2)}")


ACCOUNT_TYPES = ("Savings", "Current")


def account_open_problem(customer, account_type, check_pending=True):
    """Why this customer cannot have this account opened now, or None. Used when the request is
    raised and again at final approval (customer status or KYC may change while it waits)."""
    if account_type not in ACCOUNT_TYPES:
        return "Choose an account type: Savings or Current."
    if (customer.status or "Active") != "Active":
        return f"{customer.name}'s customer record is {customer.status.lower()}; accounts can be opened only for active customers."
    if customer.kyc_status != "Verified":
        return (f"KYC is {customer.kyc_status.lower()} for {customer.name}. "
                f"Verify PAN and Aadhaar before opening an account.")
    if check_pending:
        for acc in customer.accounts.filter_by(account_type=account_type, status="Pending"):
            req = pending_request("account_open", acc.id)
            if req:
                return (f"A {account_type} account request for {customer.name} is already waiting for approval "
                        f"({req.request_number}, account {acc.account_number}). It must be approved or rejected first.")
    return None


def _exec_account_open(req, approved):
    acc = db.session.get(Account, req.ref_id)
    if approved:
        problem = account_open_problem(acc.customer, acc.account_type, check_pending=False)
        if problem:
            raise ApprovalBlocked(problem)
    acc.status = "Active" if approved else "Rejected"
    if approved:
        activity(acc.customer_id, "Account created", acc.account_number, f"{acc.account_type} account activated")


def _exec_account_close(req, approved):
    if not approved:
        return
    acc = lock(db.session.get(Account, req.ref_id))
    problem = closure_problem(acc)
    if problem:
        raise ApprovalBlocked(problem)
    acc.status = "Closed"
    acc.closed_on = local_today()
    activity(acc.customer_id, "Account closed", acc.account_number)


def _exec_account_freeze(req, approved):
    if not approved:
        return
    acc = lock(db.session.get(Account, req.ref_id))
    if acc.status in ("Closed", "Pending", "Rejected", "Frozen"):
        raise ApprovalBlocked(f"Account is {acc.status.lower()} and cannot be frozen.")
    acc.status = "Frozen"
    activity(acc.customer_id, "Account frozen", acc.account_number, req.payload.get("reason", ""))


def _exec_account_unfreeze(req, approved):
    if not approved:
        return
    acc = lock(db.session.get(Account, req.ref_id))
    if acc.status != "Frozen":
        raise ApprovalBlocked("Account is not frozen.")
    acc.status = "Active"
    activity(acc.customer_id, "Account unfrozen", acc.account_number, req.payload.get("reason", ""))


CUSTOMER_EDITABLE = ("mobile", "email", "address", "city", "occupation", "risk_category", "tds_exempt_fy")


def _exec_customer_update(req, approved):
    if not approved:
        return
    c = lock(db.session.get(Customer, req.ref_id))
    changes = req.payload.get("changes", {})
    for field_name, value in changes.items():
        if field_name in CUSTOMER_EDITABLE:
            setattr(c, field_name, value)
    activity(c.id, "Authorized account change", c.customer_code, "Updated: " + ", ".join(sorted(changes)))


def _exec_fd_create(req, approved):
    fd = lock(db.session.get(FixedDeposit, req.ref_id))
    if approved:
        fd.status = "Active"
        activity(fd.customer_id, "FD created", fd.fd_number)
        return
    fd.status = "Rejected"
    funding = fd.account
    if funding and funding.status not in ("Closed", "Rejected"):
        post_transaction(funding, "Credit", fd.principal, "Transfer", "FD Reversal",
                         f"Refund for rejected FD {fd.fd_number}", contra=[L.Leg(L.TERM_DEPOSITS, fd.principal)])


def _exec_fd_close(req, approved):
    if not approved:
        return
    fd = lock(db.session.get(FixedDeposit, req.ref_id))
    if fd.status != "Active":
        raise ApprovalBlocked("Only an active FD can be closed early.")
    pay_out_fd(fd, premature=True)


def _exec_interest_posting(req, approved):
    if not approved:
        return
    p = req.payload
    from datetime import date as _date
    post_savings_interest(_date.fromisoformat(p["start"]), _date.fromisoformat(p["end"]), p["branch_ids"],
                          req.request_number)


def _exec_role_permissions(req, approved):
    if not approved:
        return
    role = db.session.get(Role, req.ref_id)
    apply_role_grants(role, req.payload["grants"])
    audit("Permissions updated", "user_management", role.name, f"via approval {req.request_number}")


_HANDLERS = {
    "large_txn": _exec_large_txn, "loan_sanction": _exec_loan_sanction, "loan_rate_change": _exec_loan_rate_change,
    "account_open": _exec_account_open, "account_close": _exec_account_close,
    "account_freeze": _exec_account_freeze, "account_unfreeze": _exec_account_unfreeze,
    "customer_update": _exec_customer_update, "fd_create": _exec_fd_create, "fd_close": _exec_fd_close,
    "interest_posting": _exec_interest_posting, "role_permissions": _exec_role_permissions,
    "txn_reversal": _exec_txn_reversal,
    "fee_waiver": lambda req, approved: _charges().exec_fee_waiver(req, approved),
}


def _charges():
    from . import charges  # charges imports this module; imported late to avoid a cycle
    return charges


def apply_role_grants(role, grants):
    """grants: {module: [actions]}; replaces the role's permissions."""
    from .models import ACTIONS, MODULES
    existing = {p.module: p for p in role.permissions}
    for module, _ in MODULES:
        p = existing.get(module)
        if not p:
            p = Permission(module=module)
            role.permissions.append(p)
        for a in ACTIONS:
            setattr(p, f"can_{a}", a in grants.get(module, []))


# --------------------------------------------------------------------------
# Accounts & transactions
# --------------------------------------------------------------------------

class TransferError(ValueError):
    pass


def transfer_problem(src, dst, amount):
    """Why this transfer cannot be made, or None (balances re-checked when posting)."""
    if src is None or dst is None:
        return "Both accounts must exist."
    if src.id == dst.id:
        return "Choose two different accounts."
    for acc, side in ((src, "From"), (dst, "To")):
        if acc.status in ("Pending", "Closed", "Rejected"):
            return f"{side} account {acc.account_number} is {acc.status.lower()}."
    if src.status == "Frozen":
        return f"From account {src.account_number} is frozen: withdrawals and transfers out are blocked."
    if amount is not None and q2(src.balance) < amount:
        return f"Insufficient balance in {src.account_number}: available {inr(src.balance, 2)}."
    return None


def transfer_funds(src, dst, amount, narration=""):
    """Move money between two accounts in this bank as one atomic operation: both rows are locked
    in id order (so opposite transfers cannot deadlock), then a debit and a credit are posted, each
    against clearing, so the clearing ledger nets to zero and customer deposits are unchanged."""
    for acc in sorted((src, dst), key=lambda a: a.id):
        lock(acc)
    problem = transfer_problem(src, dst, amount)
    if problem:
        raise TransferError(problem)
    note = (narration or "").strip()[:150]
    out = post_transaction(src, "Debit", amount, "Transfer", "Transfer Out",
                           f"Transfer to {dst.account_number}" + (f" — {note}" if note else ""), contra=L.CLEARING)
    into = post_transaction(dst, "Credit", amount, "Transfer", "Transfer In",
                            f"Transfer from {src.account_number}" + (f" — {note}" if note else ""), contra=L.CLEARING)
    activity(src.customer_id, "Transfer sent", out.txn_ref, f"{inr(amount, 2)} to {dst.account_number}")
    activity(dst.customer_id, "Transfer received", into.txn_ref, f"{inr(amount, 2)} from {src.account_number}")
    return out, into


def _exec_fund_transfer(req, approved):
    if not approved:
        return
    src = db.session.get(Account, req.ref_id)
    dst = db.session.get(Account, (req.payload or {}).get("to_account_id") or 0)
    try:
        transfer_funds(src, dst, req.amount, (req.payload or {}).get("narration", ""))
    except ValueError as exc:
        raise ApprovalBlocked(str(exc)) from exc


_HANDLERS["fund_transfer"] = _exec_fund_transfer


def needs_large_txn_approval(amount):
    limit = Setting.get_int("large_txn_approval_limit")
    return bool(limit) and D(amount) >= limit


def closure_problem(acc):
    """Why an account cannot be closed right now, or None."""
    if q2(acc.balance) != ZERO:
        return (f"Account {acc.account_number} still holds {inr(acc.balance, 2)}. Withdraw or transfer the "
                f"balance to zero before closing it.")
    if Loan.query.filter(Loan.account_id == acc.id, Loan.status.in_(["Active", "Overdue", "Pending"])).first():
        return f"Account {acc.account_number} is linked to a running loan. Close or relink the loan first."
    if FixedDeposit.query.filter(FixedDeposit.account_id == acc.id,
                                 FixedDeposit.status.in_(["Active", "Pending"])).first():
        return f"Account {acc.account_number} is the payout account of a running FD."
    return None


def post_transaction(account, txn_type, amount, channel, category, description, when=None, contra=None,
                     allow_frozen=False):
    """Credit or debit a customer account and post the matching ledger entry atomically.

    contra: the other side of the entry — None (cash / clearing by channel), a GL code, or
    a list of ledger.Leg for split postings (e.g. an EMI split into principal and interest).
    allow_frozen: bank-side debits (fees, TDS) that a debit freeze does not stop.
    After a credit, fees recorded as due on the account are collected (charges.collect_due_fees).
    """
    from .money import MAX_AMOUNT
    if txn_type not in ("Credit", "Debit"):
        raise ValueError("Unknown transaction type.")
    try:
        amount = q2(amount)
    except Exception as exc:  # noqa: BLE001 - anything that is not a number
        raise ValueError("Amount must be a number.") from exc
    if not amount.is_finite() or amount <= ZERO or amount > MAX_AMOUNT:
        raise ValueError("Amount must be between ₹0.01 and ₹1,000 crore.")
    lock(account)  # serialise concurrent postings on this account
    if account.status in ("Closed", "Pending", "Rejected"):
        raise ValueError("Transactions are not allowed on a closed or pending account.")
    if txn_type == "Debit" and account.status == "Frozen" and not allow_frozen:
        raise ValueError("This account is frozen: withdrawals and debits are blocked.")
    if txn_type == "Debit" and amount > q2(account.balance):
        raise ValueError(f"Insufficient balance: available {inr(account.balance, 2)}.")
    account.balance = q2(account.balance + (amount if txn_type == "Credit" else -amount))
    account.last_txn_on = ((when or datetime.utcnow()) + _tz()).date()
    prior_status = None
    if account.status in ("Dormant", "Inactive"):
        prior_status, account.status = account.status, "Active"
    txn = Transaction(
        txn_ref=unique_number(Transaction, "txn_ref", "TXN", 12), account_id=account.id,
        customer_id=account.customer_id, branch_id=account.branch_id, txn_type=txn_type,
        channel=channel, category=category, amount=amount, balance_after=account.balance,
        description=(description or "")[:255], timestamp=when or datetime.utcnow(), created_by=_actor_id(),
    )
    db.session.add(txn)
    db.session.flush()

    if contra is None:
        legs = [L.Leg(L.contra_for_channel(channel), amount)]
    elif isinstance(contra, str):
        legs = [L.Leg(contra, amount)]
    else:
        legs = list(contra)
    customer_leg = L.Leg(L.DEPOSITS, amount, account_id=account.id)
    debits, credits = (legs, [customer_leg]) if txn_type == "Credit" else ([customer_leg], legs)
    entry = L.post_journal(account.branch_id, f"{category}: {description}"[:255], debits, credits,
                           "transaction", txn.txn_ref, _actor_id())
    txn.journal_entry_id = entry.id
    evaluate_transaction(txn, prior_status)
    if txn_type == "Credit" and _charges().has_due_fees(account.id):
        _charges().collect_due_fees(account)
    return txn


def _tz():
    from .utils import TZ_OFFSET
    return TZ_OFFSET


# --------------------------------------------------------------------------
# Loans
# --------------------------------------------------------------------------

def disburse_loan(loan):
    today = local_today()
    loan.status = "Active"
    loan.disbursed_on = today
    loan.disbursed_amount = loan.amount
    loan.outstanding = loan.amount
    loan.next_due_date = add_months(today, 1)
    if loan.account:
        post_transaction(loan.account, "Credit", loan.amount, "Transfer", "Loan Disbursement",
                         f"Disbursal of {loan.loan_number}",
                         contra=[L.Leg(L.LOANS, loan.amount, loan_id=loan.id)])
    else:
        L.post_journal(loan.branch_id, f"Cash disbursal of {loan.loan_number}",
                       [L.Leg(L.LOANS, loan.amount, loan_id=loan.id)], [L.Leg(L.CASH, loan.amount)],
                       "disbursal", loan.loan_number, _actor_id())
    activity(loan.customer_id, "Loan disbursed", loan.loan_number, f"{inr(loan.amount, 2)}")


def remaining_instalments(loan):
    if not loan.disbursed_on:
        return loan.tenure_months
    maturity = add_months(loan.disbursed_on, loan.tenure_months)
    nxt = loan.next_due_date or local_today()
    months = (maturity.year - nxt.year) * 12 + (maturity.month - nxt.month) + 1
    return max(1, months)


def accrued_interest(loan, on_date):
    """Simple interest on the outstanding principal for the days since the last repayment
    (or disbursal), actual/365. A second payment on the same day carries no more interest."""
    last = (db.session.query(func.max(LoanRepayment.paid_on)).filter(LoanRepayment.loan_id == loan.id).scalar()
            or loan.disbursed_on or on_date)
    return simple_interest(loan.outstanding, loan.interest_rate, (on_date - last).days)


def penalty_due(loan, on_date):
    """Penal charge on the overdue instalment: EMI × penal rate × days late / 365, less what
    has already been paid against the same due date (so part-payments are not double-charged)."""
    if not loan.next_due_date or on_date <= loan.next_due_date or loan.status not in ("Active", "Overdue"):
        return ZERO
    rate = D(Setting.get("penal_rate_pa") or 0)
    accrued = simple_interest(loan.emi, rate, (on_date - loan.next_due_date).days)
    paid = q2(db.session.query(func.coalesce(func.sum(LoanRepayment.penalty), 0))
              .filter(LoanRepayment.loan_id == loan.id, LoanRepayment.due_date == loan.next_due_date).scalar())
    return max(ZERO, accrued - paid)


def loan_dues(loan, on_date=None):
    on_date = on_date or local_today()
    interest = accrued_interest(loan, on_date)
    penalty = penalty_due(loan, on_date)
    return {"principal": q2(loan.outstanding), "interest": interest, "penalty": penalty,
            "payoff": q2(loan.outstanding + interest + penalty)}


class RepaymentError(ValueError):
    pass


def repay_loan(loan, amount, mode):
    """Allocate a repayment (penalty → interest → principal), post it to the ledger, and
    roll the schedule forward. The caller must have locked nothing; we lock the loan."""
    lock(loan)
    amount = q2(amount)
    if loan.status not in ("Active", "Overdue"):
        raise RepaymentError("Repayment can only be posted on an active loan.")
    today = local_today()
    dues = loan_dues(loan, today)
    if amount > dues["payoff"]:
        raise RepaymentError(f"{inr(amount, 2)} is more than the amount due. The full payoff today is "
                             f"{inr(dues['payoff'], 2)} (principal {inr(dues['principal'], 2)} + interest "
                             f"{inr(dues['interest'], 2)} + penal charges {inr(dues['penalty'], 2)}).")
    penalty, interest, principal, _excess = allocate_payment(amount, dues["penalty"], dues["interest"], dues["principal"])
    late = bool(loan.next_due_date and today > loan.next_due_date)
    repayment = LoanRepayment(loan_id=loan.id, due_date=loan.next_due_date, paid_on=today, amount=amount,
                              principal_component=principal, interest_component=interest, penalty=penalty,
                              mode=mode, status="Late" if late else "Paid", created_by=_actor_id())
    db.session.add(repayment)
    legs = [L.Leg(L.LOANS, principal, loan_id=loan.id), L.Leg(L.INTEREST_INCOME, interest, loan_id=loan.id),
            L.Leg(L.PENAL_INCOME, penalty, loan_id=loan.id)]
    if mode == "Account debit":
        if not loan.account:
            raise RepaymentError("This loan has no linked account to debit.")
        post_transaction(loan.account, "Debit", amount, "Transfer", "Loan EMI", f"EMI for {loan.loan_number}",
                         contra=legs)
    else:
        L.post_journal(loan.branch_id, f"Repayment of {loan.loan_number} ({mode})",
                       [L.Leg(L.contra_for_channel("Cash" if mode == "Cash" else "NEFT"), amount)], legs,
                       "repayment", loan.loan_number, _actor_id())
    loan.outstanding = q2(loan.outstanding - principal)
    if loan.outstanding <= ZERO:
        loan.outstanding = ZERO
        loan.status = "Closed"
        loan.next_due_date = None
        loan.overdue_days = 0
        activity(loan.customer_id, "Loan closed", loan.loan_number, "Fully repaid")
    elif interest + principal >= q2(loan.emi * Decimal("0.99")):
        loan.next_due_date = add_months(loan.next_due_date or today, 1)
        if loan.status == "Overdue" and loan.next_due_date >= today:
            loan.status = "Active"
            loan.overdue_days = 0
    activity(loan.customer_id, "Payment received", loan.loan_number,
             f"{inr(amount, 2)} via {mode} (principal {inr(principal, 2)}, interest {inr(interest, 2)}, penal {inr(penalty, 2)})")
    return repayment


# --------------------------------------------------------------------------
# Fixed deposits
# --------------------------------------------------------------------------

def fd_value_on(fd, on_date, rate=None):
    """Value of an FD after the days actually run, quarterly compounding."""
    rate = D(fd.interest_rate if rate is None else rate)
    days = max((on_date - fd.start_date).days, 0)
    months = Decimal(days) * 12 / Decimal(365)
    return fd_maturity(fd.principal, rate, months) if days else q2(fd.principal)


def premature_quote(fd, on_date=None):
    on_date = on_date or local_today()
    penalty_pct = D(Setting.get("fd_premature_penalty_pct") or 0)
    rate = max(ZERO, D(fd.interest_rate) - penalty_pct)
    value = max(q2(fd.principal), fd_value_on(fd, on_date, rate))
    return {"rate": rate, "value": value, "interest": q2(value - fd.principal)}


def pay_out_fd(fd, premature=False):
    today = local_today()
    if premature:
        quote = premature_quote(fd, today)
        value, interest = quote["value"], quote["interest"]
    else:
        value, interest = q2(fd.maturity_amount), q2(fd.maturity_amount - fd.principal)
    payout = fd.account if fd.account and fd.account.status not in ("Closed", "Rejected", "Pending") else None
    if not payout:
        raise ApprovalBlocked("No open payout account is linked to this FD.")
    post_transaction(payout, "Credit", value, "Transfer", "FD Maturity" if not premature else "FD Premature Closure",
                     f"{'Premature closure' if premature else 'Maturity proceeds'} of {fd.fd_number}",
                     contra=[leg for leg in (L.Leg(L.TERM_DEPOSITS, fd.principal), L.Leg(L.INTEREST_EXPENSE, interest))
                             if leg.amount > ZERO])
    if interest > ZERO:
        _charges().deduct_tds_on_fd_interest(fd, payout, interest)
    fd.status = "Closed" if premature else "Matured"
    fd.premature = premature
    fd.paid_out_on = today
    fd.paid_out_amount = value
    activity(fd.customer_id, "FD closed early and paid out" if premature else "FD matured and paid out",
             fd.fd_number, f"{inr(value, 2)} credited to {payout.account_number}")


# --------------------------------------------------------------------------
# Savings interest (actual/365 on daily closing balances)
# --------------------------------------------------------------------------

def _balance_products(start, end, statuses, branch_ids=None, min_rate=-1, opened_by=None):
    """(account id, number, rate, product) for savings accounts in `statuses` (and `branch_ids`, all
    if None; rate above `min_rate`; opened on or before `opened_by`), where product = Σ daily closing balance over local dates start..end, from the transaction history:
    opening × days + Σ net_i × (end − day_i + 1) for each transaction i in the period,
    and opening = today's balance − net of every transaction on or after `start`."""
    from .utils import TZ_OFFSET, day_bounds
    s_utc, _ = day_bounds(start, start)
    days = (end - start).days + 1
    off = int(TZ_OFFSET.total_seconds() // 60)
    sql = db.text("""
        SELECT a.id, a.account_number, a.interest_rate, a.balance,
               a.balance - COALESCE(SUM(CASE WHEN t.timestamp >= :s THEN
                        CASE WHEN t.txn_type = 'Credit' THEN t.amount ELSE -t.amount END END), 0) AS opening,
               COALESCE(SUM(CASE WHEN t.timestamp >= :s
                                  AND (t.timestamp + make_interval(mins => :off))::date <= :e THEN
                        (CASE WHEN t.txn_type = 'Credit' THEN t.amount ELSE -t.amount END)
                        * (:e - (t.timestamp + make_interval(mins => :off))::date + 1) END), 0) AS weighted
        FROM account a LEFT JOIN transaction t ON t.account_id = a.id AND t.timestamp >= :s
        WHERE a.account_type = 'Savings' AND a.status = ANY(:st) AND a.interest_rate > :min_rate
          AND (:all_branches OR a.branch_id = ANY(:b)) AND (:any_open_date OR a.opened_on <= :opened_by)
        GROUP BY a.id ORDER BY a.id""")
    rows = db.session.execute(sql, {"s": s_utc, "e": end, "off": off, "st": list(statuses), "min_rate": min_rate,
                                    "all_branches": branch_ids is None, "b": list(branch_ids or []),
                                    "any_open_date": opened_by is None, "opened_by": opened_by or end}).all()
    return [(acc_id, number, rate, D(opening) * days + D(weighted))
            for acc_id, number, rate, _balance, opening, weighted in rows]


def average_daily_balances(start, end, statuses, opened_by=None):
    """[(savings account id, average daily closing balance)] for local dates start..end."""
    days = (end - start).days + 1
    return [(acc_id, q2(product / days))
            for acc_id, _n, _r, product in _balance_products(start, end, statuses, opened_by=opened_by)]


def savings_interest_preview(start, end, branch_ids):
    """Interest per savings account for local dates start..end: Σ daily closing balance × rate / 365."""
    days = (end - start).days + 1
    rows = _balance_products(start, end, ("Active", "Inactive", "Dormant", "Frozen"), branch_ids=branch_ids, min_rate=0)
    out = []
    for acc_id, number, rate, product in rows:
        interest = q2(product * D(rate) / 100 / 365) if product > 0 else ZERO
        if interest > ZERO:
            out.append({"account_id": acc_id, "account_number": number, "rate": rate,
                        "avg_balance": q2(product / days), "interest": interest})
    return out


def post_savings_interest(start, end, branch_ids, reference):
    from .models import InterestRun
    for b in branch_ids:
        if InterestRun.query.filter_by(branch_id=b, period_start=start, period_end=end).first():
            raise ApprovalBlocked(f"Interest for {start}–{end} was already posted for branch #{b}.")
    rows = savings_interest_preview(start, end, branch_ids)
    totals, counts = {}, {}
    for r in rows:
        acc = db.session.get(Account, r["account_id"])
        post_transaction(acc, "Credit", r["interest"], "Transfer", "Interest",
                         f"Savings interest {start:%d %b} – {end:%d %b %Y}", contra=L.INTEREST_EXPENSE)
        totals[acc.branch_id] = totals.get(acc.branch_id, ZERO) + r["interest"]
        counts[acc.branch_id] = counts.get(acc.branch_id, 0) + 1
    for b in branch_ids:
        db.session.add(InterestRun(branch_id=b, period_start=start, period_end=end, reference=reference,
                                   total=totals.get(b, ZERO), accounts=counts.get(b, 0)))
    return rows


# --------------------------------------------------------------------------
# Alerts & risk rules
# --------------------------------------------------------------------------

def raise_alert(alert_type, title, message, module="alerts", severity="Medium", reference=None,
                link=None, customer_id=None, branch_id=None, dedupe=True):
    """Create an alert. The partial unique index on open (type, reference) makes duplicates
    impossible even when two requests raise the same alert at once."""
    if dedupe and reference:
        exists = Alert.query.filter_by(alert_type=alert_type, reference=reference, is_resolved=False).first()
        if exists:
            return exists
    alert = Alert(alert_type=alert_type, title=title[:200], message=(message or "")[:500], module=module,
                  severity=severity, reference=reference, link=link, customer_id=customer_id, branch_id=branch_id)
    try:
        with db.session.begin_nested():
            db.session.add(alert)
    except IntegrityError:
        return Alert.query.filter_by(alert_type=alert_type, reference=reference, is_resolved=False).first()
    return alert


def evaluate_transaction(txn, prior_status=None):
    for rule in AlertRule.query.filter_by(enabled=True).all():
        if rule.rule_type == "high_value_txn" and txn.amount >= rule.threshold:
            txn.flagged = True
            raise_alert("fraud_risk", f"High-value transaction {txn.txn_ref}",
                        f"{txn.txn_type} of {inr(txn.amount)} via {txn.channel} exceeds the "
                        f"{inr(rule.threshold)} threshold ({rule.name}).",
                        module=rule.route_module, severity=rule.severity, reference=txn.txn_ref,
                        link=f"/customers/{txn.customer_id}?tab=transactions",
                        customer_id=txn.customer_id, branch_id=txn.branch_id)
        elif rule.rule_type == "txn_velocity":
            since = txn.timestamp - timedelta(minutes=rule.window_minutes)
            count = Transaction.query.filter(Transaction.account_id == txn.account_id,
                                             Transaction.timestamp >= since).count()
            if count >= rule.threshold:
                txn.flagged = True
                raise_alert("unusual_txn", "Rapid transactions on account",
                            f"{count} transactions within {rule.window_minutes} min on account "
                            f"{txn.account.account_number}.", module=rule.route_module,
                            severity=rule.severity, reference=f"VEL-{txn.account_id}",
                            link=f"/customers/{txn.customer_id}?tab=transactions",
                            customer_id=txn.customer_id, branch_id=txn.branch_id)
        elif rule.rule_type == "dormant_activity" and prior_status == "Dormant":
            txn.flagged = True
            raise_alert("unusual_txn", "Activity on dormant account",
                        f"Dormant account {txn.account.account_number} received a "
                        f"{txn.txn_type.lower()} of {inr(txn.amount)}.", module=rule.route_module,
                        severity=rule.severity, reference=txn.txn_ref,
                        link=f"/customers/{txn.customer_id}?tab=transactions",
                        customer_id=txn.customer_id, branch_id=txn.branch_id)


def failed_login_alert(email, attempts, employee=None):
    rule = AlertRule.query.filter_by(rule_type="failed_logins", enabled=True).first()
    threshold = int(rule.threshold) if rule else 3
    if attempts >= threshold:
        raise_alert("failed_login", "Multiple failed sign-ins for an employee account",
                    f"{attempts} consecutive failed sign-in attempts for {email}.", module="user_management",
                    severity=rule.severity if rule else "High",
                    reference=f"LOGIN-{email}-{local_today().isoformat()}",
                    link="/users/login-history", branch_id=employee.branch_id if employee else None)


SCAN_LOCK_ID = 7_001_002  # advisory lock: one scan at a time across every app server


def scan_scheduled_alerts(force=False):
    """EMI due, overdue loans, KYC/document expiry, FD maturity payout, dormancy, audit sealing.
    Runs at most hourly unless forced; only one server runs it at a time."""
    from sqlalchemy import literal
    from sqlalchemy.orm import joinedload

    last = Setting.get("last_alert_scan")
    if not force and last:
        try:
            if datetime.utcnow() - datetime.fromisoformat(last) < timedelta(hours=1):
                return False
        except ValueError:
            pass
    if not db.session.execute(db.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SCAN_LOCK_ID}).scalar():
        return False
    today = local_today()
    emi_days = Setting.get_int("emi_due_warning_days")
    kyc_days = Setting.get_int("kyc_expiry_warning_days")

    Loan.query.filter(Loan.status == "Active", Loan.next_due_date < today).update(
        {"status": "Overdue"}, synchronize_session=False)
    Loan.query.filter(Loan.status == "Overdue", Loan.next_due_date != None).update(  # noqa: E711
        {"overdue_days": func.greatest(literal(today) - Loan.next_due_date, 0)}, synchronize_session=False)

    open_refs = {r for (r,) in db.session.query(Alert.reference).filter(Alert.is_resolved.is_(False))}
    new_alerts = []
    now = datetime.utcnow()

    def add_alert(reference, alert_type, title, message, module="alerts", severity="Medium", link=None,
                  customer_id=None, branch_id=None):
        # Collected and inserted in batches below: one round trip per alert made the first scan
        # over 6,000 loans take 59 s (measured), almost all of it in per-alert savepoints.
        if reference not in open_refs:
            open_refs.add(reference)
            new_alerts.append({"alert_type": alert_type, "title": title[:200], "message": (message or "")[:500],
                               "module": module, "severity": severity, "reference": reference, "link": link,
                               "customer_id": customer_id, "branch_id": branch_id, "is_read": False,
                               "is_resolved": False, "created_at": now})

    for loan in (Loan.query.options(joinedload(Loan.customer))
                 .filter(Loan.status == "Active", Loan.next_due_date != None,  # noqa: E711
                         Loan.next_due_date <= today + timedelta(days=emi_days))):
        add_alert(f"EMI-{loan.loan_number}-{loan.next_due_date}", alert_type="emi_due",
                  title=f"EMI due for {loan.loan_number}",
                  message=f"{loan.customer.name}: EMI {inr(loan.emi)} due on {loan.next_due_date:%d %b %Y}.",
                  module="loans", severity="Low", link=f"/loans/{loan.id}",
                  customer_id=loan.customer_id, branch_id=loan.branch_id)
    for loan in Loan.query.options(joinedload(Loan.customer)).filter(Loan.status == "Overdue"):
        add_alert(f"OD-{loan.loan_number}", alert_type="overdue_loan", title=f"Overdue loan {loan.loan_number}",
                  message=f"{loan.customer.name}: {loan.overdue_days} days overdue, outstanding {inr(loan.outstanding)}.",
                  module="loans", severity="High" if (loan.overdue_days or 0) > 60 else "Medium",
                  link=f"/loans/{loan.id}", customer_id=loan.customer_id, branch_id=loan.branch_id)
    for doc in (Document.query.options(joinedload(Document.customer))
                .filter(Document.expiry_date != None,  # noqa: E711
                        Document.expiry_date <= today + timedelta(days=kyc_days),
                        Document.status.in_(["Verified", "Pending"]))):
        expired = doc.expiry_date < today
        if expired:
            doc.status = "Expired"
            if doc.category == "KYC":
                doc.customer.kyc_status = "Expired"
        add_alert(f"DOC-{doc.id}-{doc.expiry_date}", alert_type="kyc_expiry" if doc.category == "KYC" else "document_expiry",
                  title=f"{doc.doc_type} {'expired' if expired else 'expiring'}",
                  message=f"{doc.customer.name}: {doc.doc_type} {'expired' if expired else 'expires'} on {doc.expiry_date:%d %b %Y}.",
                  module="kyc", severity="High" if expired else "Medium",
                  link=f"/customers/{doc.customer_id}?tab=documents",
                  customer_id=doc.customer_id, branch_id=doc.customer.branch_id)
    for fd in (FixedDeposit.query.filter(FixedDeposit.status == "Active", FixedDeposit.maturity_date <= today)
               .with_for_update(skip_locked=True).all()):
        try:
            with db.session.begin_nested():
                pay_out_fd(fd)
        except (ApprovalBlocked, ValueError):
            fd.status = "Matured"
            add_alert(f"FDPAY-{fd.fd_number}", alert_type="system", title=f"FD {fd.fd_number} matured — payout needs action",
                      message=f"No open payout account is linked. Pay {inr(fd.maturity_amount)} to the customer manually.",
                      module="deposits", severity="High", customer_id=fd.customer_id, branch_id=fd.branch_id)
    _insert_alerts(new_alerts)
    from .models import FormToken
    FormToken.query.filter(FormToken.used_at < datetime.utcnow() - timedelta(days=2)).delete(synchronize_session=False)
    Account.query.filter(Account.status == "Active", Account.last_txn_on < today - timedelta(days=365)).update(
        {"status": "Dormant"}, synchronize_session=False)
    Account.query.filter(Account.status == "Active", Account.last_txn_on < today - timedelta(days=180)).update(
        {"status": "Inactive"}, synchronize_session=False)
    period = f"{today.replace(day=1) - timedelta(days=1):%Y-%m}"
    if Setting.get("fees_charged_for") != period:  # monthly minimum-balance charges, once per month
        _charges().charge_monthly_fees(today)
        Setting.set("fees_charged_for", period)
    Setting.set("last_alert_scan", datetime.utcnow().isoformat())
    db.session.commit()
    from .audit_chain import seal
    seal()
    return True


def _insert_alerts(rows, batch=500):
    """Insert many alerts in a few statements. The partial unique index on open (type, reference)
    still guarantees no duplicate open alert: a row that would collide is skipped by the database."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(Alert.__table__).on_conflict_do_nothing(
        index_elements=["alert_type", "reference"],
        index_where=db.text("is_resolved = false AND reference IS NOT NULL"))
    for i in range(0, len(rows), batch):
        db.session.execute(stmt, rows[i:i + batch])  # executemany: the driver batches the rows


_scan_lock = __import__("threading").Lock()


def start_background_scan(app):
    """Run the hourly scan on a worker thread if it is due. Returns immediately. Disable with
    BANK_INPROCESS_SCAN=0 when a dedicated scheduler runs `flask scan`."""
    import os
    import threading
    if os.environ.get("BANK_INPROCESS_SCAN", "1") == "0":
        return
    last = Setting.get("last_alert_scan")
    try:
        if last and datetime.utcnow() - datetime.fromisoformat(last) < timedelta(hours=1):
            return
    except ValueError:
        pass
    if not _scan_lock.acquire(blocking=False):
        return

    def run():
        try:
            with app.app_context():
                scan_scheduled_alerts()
        except Exception:  # noqa: BLE001
            app.logger.exception("Background scan failed")
        finally:
            with app.app_context():
                db.session.remove()
            _scan_lock.release()
    threading.Thread(target=run, name="bank-scan", daemon=True).start()


def severity_order():
    return db.case({"Critical": 0, "High": 1, "Medium": 2}, value=Alert.severity, else_=3)


def visible_alerts(user):
    """Alerts are routed to employees who can view the alert's module, scoped by branch."""
    from .models import MODULES
    allowed = [m for m, _ in MODULES if user.can(m, "view")]
    q = Alert.query.filter(Alert.module.in_(allowed))
    if not user.sees_all_branches:
        q = q.filter(or_(Alert.branch_id == user.branch_id, Alert.branch_id == None))  # noqa: E711
    return q


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def search_customers(term, base_query):
    """Server-side search: exact on customer ID, account number, loan number, PAN and mobile
    (via keyed hashes of the encrypted values); trigram-indexed partial match on name."""
    from .crypto import search_hash
    term = (term or "").strip()[:80]
    if not term:
        return base_query.filter(False)
    h = search_hash(term)
    acc_cust = db.session.query(Account.customer_id).filter(Account.account_number == term)
    loan_cust = db.session.query(Loan.customer_id).filter(Loan.loan_number == term.upper())
    conds = [Customer.customer_code == term.upper(), Customer.pan_hash == h, Customer.mobile_hash == h,
             Customer.id.in_(acc_cust), Customer.id.in_(loan_cust)]
    if len(term) >= 3 and not term.isdigit():
        conds.append(Customer.name.ilike(f"%{term}%"))
    return base_query.filter(or_(*conds))


def money_sum(column, *filters):
    return q2(db.session.query(func.coalesce(func.sum(column), 0)).filter(*filters).scalar())


def employee_branch_ids(user):
    from .models import Branch
    return [b.id for b in Branch.query.all()] if user.sees_all_branches else [user.branch_id]

