"""Fees & charges, and TDS (tax deducted at source) on deposit interest.

Fees
----
* A FeeType is a flat amount or a percentage of a base amount (with optional min / max), plus GST.
* Charging debits the customer account: the fee goes to GL 4200 (fee income), GST to GL 2300.
* A service fee (cheque book, duplicate statement) that the balance cannot cover is recorded as
  **Due** — no ledger entry, the balance never goes negative — and is collected automatically
  from the next credit to that account, oldest first.
* The monthly minimum-balance charge is never recorded as Due (RBI: penal charges for not keeping
  the minimum balance must not turn the balance negative): if the balance cannot cover it, it is
  recorded as Skipped. Only Active savings accounts are charged, never in the month they opened.
* A waiver is a maker-checker request; approving it refunds a charged fee (reversing both GL legs)
  or cancels a due one.

TDS
---
* Applies to fixed-deposit interest when it is paid out (maturity or premature closure).
  Savings-account interest is not subject to TDS for banks, so it is not touched.
* Per customer and financial year (April-March), all FD interest paid is added up. Once the total
  passes the threshold (setting; ₹1,00,000 for senior citizens), tax is deducted on the whole year's
  interest at the TDS rate (a higher rate without a PAN), less what was already deducted that year.
* No TDS when a Form 15G/15H declaration is on file for that financial year.
* The deduction is a separate debit on the customer's account, credited to GL 2400 (TDS payable),
  so the statement shows the gross interest and the tax separately.
* Limitation: tax is worked out when interest is paid, not on yearly accrual of a multi-year FD.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from flask import has_request_context
from flask_login import current_user

from . import ledger as L
from .extensions import db
from .models import Account, Customer, FeeCharge, FeeType, InterestTds, Setting
from .money import HUNDRED, ZERO, D, q2
from .security import activity, audit
from .utils import local_today, unique_number, inr

PAN_RE = re.compile(r"[A-Z]{5}\d{4}[A-Z]")


def _actor_id():
    return current_user.id if has_request_context() and current_user.is_authenticated else None


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------

def fee_amount(ft, base=None):
    """(fee, gst, total) for fee type `ft`; `base` is the amount a percentage fee applies to."""
    if ft.calc == "percent":
        fee = q2(D(base) * D(ft.percent) / HUNDRED)
        if ft.min_amount is not None:
            fee = max(fee, q2(ft.min_amount))
        if ft.max_amount is not None:
            fee = min(fee, q2(ft.max_amount))
    else:
        fee = q2(ft.amount)
    gst = q2(fee * D(ft.gst_pct or 0) / HUNDRED)
    return fee, gst, fee + gst


def _post_fee(account, charge, ft):
    from .services import post_transaction
    txn = post_transaction(account, "Debit", charge.total, "Transfer", "Fee",
                           f"{ft.name}" + (f" ({charge.period})" if charge.period else "")
                           + (f" incl. GST {inr(charge.gst_amount, 2)}" if charge.gst_amount else ""),
                           contra=[leg for leg in (L.Leg(L.FEE_INCOME, charge.base_amount),
                                                   L.Leg(L.GST_PAYABLE, charge.gst_amount)) if leg.amount > ZERO],
                           allow_frozen=True)
    charge.status, charge.txn_id, charge.settled_at = "Charged", txn.id, datetime.utcnow()
    return txn


def charge_fee(account, ft, base=None, period=None, note=""):
    """Charge fee type `ft` to `account`. Returns the FeeCharge (Charged, Due or Skipped).
    The caller holds the transaction; the account row is locked here."""
    from .services import lock
    if not ft.active:
        raise ValueError(f"The fee '{ft.name}' is switched off.")
    lock(account)
    if account.status in ("Closed", "Pending", "Rejected"):
        raise ValueError("Fees cannot be charged to a closed or pending account.")
    if ft.calc == "percent" and (base is None or D(base) <= ZERO):
        raise ValueError(f"Enter the amount the {ft.percent}% fee applies to.")
    fee, gst, total = fee_amount(ft, base)
    if total <= ZERO:
        raise ValueError("This fee works out to ₹0; nothing to charge.")
    charge = FeeCharge(charge_ref=unique_number(FeeCharge, "charge_ref", "FEE", 10), fee_type_id=ft.id,
                       account_id=account.id, customer_id=account.customer_id, branch_id=account.branch_id,
                       base_amount=fee, gst_amount=gst, total=total, period=period, status="Due",
                       note=(note or "")[:200], created_by=_actor_id())
    db.session.add(charge)
    db.session.flush()
    if total <= q2(account.balance):
        _post_fee(account, charge, ft)
        activity(account.customer_id, "Fee charged", charge.charge_ref, f"{ft.name}: {inr(total, 2)}")
    elif ft.kind == "min_balance":
        charge.status = "Skipped"
        charge.note = "Not charged: balance too low (the charge may not make the balance negative)."
    else:
        activity(account.customer_id, "Fee due", charge.charge_ref,
                 f"{ft.name}: {inr(total, 2)} — collected from the next credit")
    return charge


def collect_due_fees(account):
    """Called after every credit: settle Due fees on this account, oldest first, while the
    balance covers them. Never part-collects a fee."""
    dues = (FeeCharge.query.filter_by(account_id=account.id, status="Due").order_by(FeeCharge.id)
            .with_for_update().all())
    for charge in dues:
        if charge.total > q2(account.balance):
            break
        _post_fee(account, charge, charge.fee_type)
        activity(account.customer_id, "Due fee collected", charge.charge_ref,
                 f"{charge.fee_type.name}: {inr(charge.total, 2)}")


def has_due_fees(account_id):
    return db.session.query(FeeCharge.query.filter_by(account_id=account_id, status="Due").exists()).scalar()


def waive_problem(charge):
    if charge.status not in ("Charged", "Due"):
        return f"Charge {charge.charge_ref} is {charge.status.lower()}; only charged or due fees can be waived."
    if charge.status == "Charged" and charge.account.status in ("Closed", "Rejected"):
        return "The account is closed, so the fee cannot be refunded to it."
    return None


def exec_fee_waiver(req, approved):
    """Approval handler for 'fee_waiver'."""
    from .services import ApprovalBlocked, lock, post_transaction
    if not approved:
        return
    charge = db.session.get(FeeCharge, req.ref_id)
    db.session.refresh(charge, with_for_update=True)
    problem = waive_problem(charge)
    if problem:
        raise ApprovalBlocked(problem)
    if charge.status == "Charged":
        acc = lock(charge.account)
        txn = post_transaction(acc, "Credit", charge.total, "Transfer", "Fee Refund",
                               f"Waiver of {charge.fee_type.name} ({charge.charge_ref})",
                               contra=[leg for leg in (L.Leg(L.FEE_INCOME, charge.base_amount),
                                                       L.Leg(L.GST_PAYABLE, charge.gst_amount)) if leg.amount > ZERO])
        charge.refund_txn_id = txn.id
    charge.status = "Waived"
    charge.note = (f"Waived via {req.request_number}: " + (req.payload.get("reason") or ""))[:200]
    activity(charge.customer_id, "Fee waived", charge.charge_ref, f"{inr(charge.total, 2)} via {req.request_number}")


def charge_monthly_fees(today=None):
    """Minimum-balance charge for the previous calendar month, once per account and month
    (a unique key on account + fee + period makes a rerun harmless). Returns charges created."""
    from .services import average_daily_balances
    today = today or local_today()
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    period = f"{start:%Y-%m}"
    made = 0
    for ft in FeeType.query.filter_by(kind="min_balance", active=True).all():
        done = {a for (a,) in db.session.query(FeeCharge.account_id).filter_by(fee_type_id=ft.id, period=period)}
        rows = average_daily_balances(start, end, ("Active",), opened_by=start)
        for acc_id, avg in rows:
            if acc_id in done or avg >= q2(ft.threshold or 0):
                continue
            with db.session.begin_nested():
                charge_fee(db.session.get(Account, acc_id), ft, period=period,
                           note=f"Average balance {inr(avg, 2)} below {inr(ft.threshold, 2)} in {start:%b %Y}")
            made += 1
    if made:
        audit("Monthly fees charged", "transactions", period, f"{made} charges")
    return made


DEFAULT_FEE_TYPES = [
    # code, name, calc, amount, percent, min, max, gst, kind, threshold — placeholders: set your bank's own
    ("CHQBOOK", "Cheque book (25 leaves)", "flat", "100", None, None, None, "18", "manual", None),
    ("DUPSTMT", "Duplicate statement", "flat", "50", None, None, None, "18", "manual", None),
    ("DDISSUE", "Demand draft issue", "percent", None, "0.25", "25", "5000", "18", "manual", None),
    ("MINBAL", "Minimum balance not maintained (savings)", "flat", "100", None, None, None, "18", "min_balance", "1000"),
]


def ensure_fee_types():
    """Seed the starter fee schedule once (all editable in Fees → Fee schedule)."""
    if FeeType.query.first():
        return
    for code, name, calc, amount, pct, lo, hi, gst, kind, threshold in DEFAULT_FEE_TYPES:
        db.session.add(FeeType(code=code, name=name, calc=calc, amount=D(amount) if amount else None,
                               percent=D(pct) if pct else None, min_amount=D(lo) if lo else None,
                               max_amount=D(hi) if hi else None, gst_pct=D(gst), kind=kind,
                               threshold=D(threshold) if threshold else None, active=True))


# --------------------------------------------------------------------------
# TDS on FD interest
# --------------------------------------------------------------------------

def financial_year(on: date) -> str:
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def is_senior(customer, fy: str) -> bool:
    """60 or older at any time during the financial year (i.e. by 31 March at its end)."""
    if not customer.dob:
        return False
    end_year = int(fy[:4]) + 1
    return customer.dob <= date(end_year - 60, 3, 31)


def tds_quote(customer, interest, on=None):
    """What TDS applies to `interest` paid to `customer` on date `on`. Returns a dict with tds,
    rate, fy, threshold, the year's interest so far and the reason no tax was deducted (if any)."""
    on = on or local_today()
    interest = q2(interest)
    fy = financial_year(on)
    prior_interest, prior_tds = db.session.query(
        db.func.coalesce(db.func.sum(InterestTds.interest), 0), db.func.coalesce(db.func.sum(InterestTds.tds), 0)
    ).filter(InterestTds.customer_id == customer.id, InterestTds.fin_year == fy).one()
    prior_interest, prior_tds = q2(prior_interest), q2(prior_tds)
    senior = is_senior(customer, fy)
    threshold = D(Setting.get("tds_threshold_senior" if senior else "tds_threshold"))
    pan_ok = bool(PAN_RE.fullmatch(customer.pan or ""))
    rate = D(Setting.get("tds_rate_pct" if pan_ok else "tds_rate_no_pan_pct"))
    total = prior_interest + interest
    out = {"fy": fy, "senior": senior, "threshold": threshold, "pan_ok": pan_ok, "rate": rate,
           "year_interest": total, "tds": ZERO, "reason": None}
    if interest <= ZERO:
        out["reason"] = "no interest"
    elif customer.tds_exempt_fy == fy:
        out["reason"] = f"Form {'15H' if senior else '15G'} on file for {fy}"
    elif total <= threshold:
        out["reason"] = f"year's interest {inr(total, 2)} is within the {inr(threshold, 2)} threshold"
    else:
        out["tds"] = max(ZERO, min(interest, q2(total * rate / HUNDRED) - prior_tds))
    return out


def deduct_tds_on_fd_interest(fd, account, interest):
    """Record the interest paid and deduct any TDS from `account` (already credited with it)."""
    from .services import post_transaction
    customer = db.session.get(Customer, fd.customer_id)
    db.session.refresh(customer, with_for_update=True)  # one payout per customer at a time: totals stay right
    quote = tds_quote(customer, interest)
    row = InterestTds(customer_id=customer.id, branch_id=fd.branch_id, fd_id=fd.id, fin_year=quote["fy"],
                      interest=q2(interest), tds=quote["tds"], rate=quote["rate"] if quote["tds"] else ZERO,
                      pan_available=quote["pan_ok"], note=(quote["reason"] or "")[:200])
    db.session.add(row)
    if quote["tds"] > ZERO:
        txn = post_transaction(account, "Debit", quote["tds"], "Transfer", "TDS",
                               f"TDS @ {quote['rate']:g}% on interest of {fd.fd_number} (FY {quote['fy']})",
                               contra=L.TDS_PAYABLE, allow_frozen=True)
        row.txn_id = txn.id
        activity(customer.id, "TDS deducted", fd.fd_number, f"{inr(quote['tds'], 2)} on interest {inr(interest, 2)}")
    db.session.flush()
    return row
