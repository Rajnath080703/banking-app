"""Double-entry general ledger.

Every money movement in the bank is a JournalEntry whose lines balance (total debits =
total credits). The balance rule is checked here *and* by a deferred PostgreSQL constraint
trigger, so even code that bypasses this module cannot commit an unbalanced entry.

Chart of accounts (see models.GL_ACCOUNTS):
    1000 Cash · 1100 Clearing · 1200 Loans            (assets)
    2000 Customer deposits · 2100 Term deposits       (liabilities)
    2300 GST payable · 2400 TDS payable               (liabilities)
    4000 Interest income · 4100 Penal income · 4200 Fee income (income)
    5000 Interest expense                             (expense)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, func

from .extensions import db
from .models import GL_ACCOUNTS, Account, FixedDeposit, GLAccount, JournalEntry, JournalLine, Loan
from .money import ZERO, q2
from .utils import local_today, unique_number

CASH, CLEARING, LOANS = "1000", "1100", "1200"
DEPOSITS, TERM_DEPOSITS = "2000", "2100"
INTEREST_INCOME, PENAL_INCOME, INTEREST_EXPENSE = "4000", "4100", "5000"
FEE_INCOME, GST_PAYABLE, TDS_PAYABLE = "4200", "2300", "2400"


class LedgerError(Exception):
    pass


@dataclass
class Leg:
    """One side of an entry: debit or credit `amount` to `gl`, optionally tagged to a
    customer account (deposit sub-ledger) or a loan (loan sub-ledger)."""
    gl: str
    amount: object
    account_id: int | None = None
    loan_id: int | None = None
    extra: dict = field(default_factory=dict)


def contra_for_channel(channel: str) -> str:
    return CASH if (channel or "Cash") == "Cash" else CLEARING


def ensure_chart():
    existing = {c for (c,) in db.session.query(GLAccount.code)}
    for code, name, gl_type in GL_ACCOUNTS:
        if code not in existing:
            db.session.add(GLAccount(code=code, name=name, gl_type=gl_type))


def post_journal(branch_id, narration, debits, credits, source_type, source_ref, created_by=None, value_date=None):
    """Write one balanced entry. Zero-amount legs are dropped; anything else invalid raises."""
    debits = [leg for leg in debits if q2(leg.amount) != ZERO]
    credits = [leg for leg in credits if q2(leg.amount) != ZERO]
    if not debits or not credits:
        raise LedgerError("A journal entry needs at least one debit and one credit.")
    for leg in debits + credits:
        if q2(leg.amount) < ZERO:
            raise LedgerError("Journal amounts must be positive.")
    total_dr = sum(q2(leg.amount) for leg in debits)
    total_cr = sum(q2(leg.amount) for leg in credits)
    if total_dr != total_cr:
        raise LedgerError(f"Unbalanced entry: debits {total_dr} ≠ credits {total_cr}.")
    entry = JournalEntry(entry_ref=unique_number(JournalEntry, "entry_ref", "JE", 12), branch_id=branch_id,
                         narration=narration[:255], source_type=source_type, source_ref=source_ref,
                         created_by=created_by, value_date=value_date or local_today())
    for leg in debits:
        entry.lines.append(JournalLine(gl_code=leg.gl, branch_id=branch_id, account_id=leg.account_id,
                                       loan_id=leg.loan_id, debit=q2(leg.amount), credit=ZERO))
    for leg in credits:
        entry.lines.append(JournalLine(gl_code=leg.gl, branch_id=branch_id, account_id=leg.account_id,
                                       loan_id=leg.loan_id, debit=ZERO, credit=q2(leg.amount)))
    db.session.add(entry)
    db.session.flush()
    return entry


# --------------------------------------------------------------------------
# Reporting: trial balance and reconciliation
# --------------------------------------------------------------------------

def trial_balance(branch_ids=None, as_of=None):
    q = (db.session.query(GLAccount.code, GLAccount.name, GLAccount.gl_type,
                          func.coalesce(func.sum(JournalLine.debit), 0),
                          func.coalesce(func.sum(JournalLine.credit), 0))
         .outerjoin(JournalLine, JournalLine.gl_code == GLAccount.code))
    if branch_ids is not None or as_of is not None:
        cond = []
        if branch_ids is not None:
            cond.append(JournalLine.branch_id.in_(branch_ids))
        if as_of is not None:
            cond.append(JournalLine.entry_id.in_(db.session.query(JournalEntry.id).filter(JournalEntry.value_date <= as_of)))
        q = (db.session.query(GLAccount.code, GLAccount.name, GLAccount.gl_type,
                              func.coalesce(func.sum(case((db.and_(*cond), JournalLine.debit), else_=0)), 0),
                              func.coalesce(func.sum(case((db.and_(*cond), JournalLine.credit), else_=0)), 0))
             .outerjoin(JournalLine, JournalLine.gl_code == GLAccount.code))
    rows = q.group_by(GLAccount.code, GLAccount.name, GLAccount.gl_type).order_by(GLAccount.code).all()
    out = []
    for code, name, gl_type, dr, cr in rows:
        dr, cr = q2(dr), q2(cr)
        balance = dr - cr if gl_type in ("Asset", "Expense") else cr - dr
        out.append({"code": code, "name": name, "type": gl_type, "debit": dr, "credit": cr, "balance": balance})
    return out


def gl_balance(code, branch_ids=None):
    q = db.session.query(func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0)) \
        .filter(JournalLine.gl_code == code)
    if branch_ids is not None:
        q = q.filter(JournalLine.branch_id.in_(branch_ids))
    dr, cr = q.one()
    return q2(dr), q2(cr)


def reconciliation(branch_ids=None):
    """Prove that the product ledgers (account balances, loan outstanding, FD principal)
    agree with the general ledger. Returns a list of checks with pass/fail."""
    def scope(model, q):
        return q.filter(model.branch_id.in_(branch_ids)) if branch_ids is not None else q

    tb = trial_balance(branch_ids)
    total_dr = sum(r["debit"] for r in tb)
    total_cr = sum(r["credit"] for r in tb)
    checks = [{"name": "Trial balance (total debits = total credits)", "ledger": total_dr, "books": total_cr}]

    dr, cr = gl_balance(DEPOSITS, branch_ids)
    acc_total = q2(scope(Account, db.session.query(func.coalesce(func.sum(Account.balance), 0))).scalar())
    checks.append({"name": "Customer deposits GL 2000 = sum of account balances", "ledger": cr - dr, "books": acc_total})

    dr, cr = gl_balance(LOANS, branch_ids)
    loan_total = q2(scope(Loan, db.session.query(func.coalesce(func.sum(Loan.outstanding), 0))).scalar())
    checks.append({"name": "Loans GL 1200 = sum of outstanding principal", "ledger": dr - cr, "books": loan_total})

    dr, cr = gl_balance(TERM_DEPOSITS, branch_ids)
    fd_total = q2(scope(FixedDeposit, db.session.query(func.coalesce(func.sum(FixedDeposit.principal), 0))
                        .filter(FixedDeposit.status.in_(["Pending", "Active"]))).scalar())
    checks.append({"name": "Term deposits GL 2100 = principal of open FDs", "ledger": cr - dr, "books": fd_total})

    # Sub-ledger: every individual account's ledger movement equals its balance.
    sub = (db.session.query(JournalLine.account_id,
                            func.sum(JournalLine.credit) - func.sum(JournalLine.debit))
           .filter(JournalLine.gl_code == DEPOSITS, JournalLine.account_id != None)  # noqa: E711
           .group_by(JournalLine.account_id).subquery())
    mismatched = scope(Account, db.session.query(func.count(Account.id))
                       .outerjoin(sub, sub.c.account_id == Account.id)
                       .filter(func.coalesce(sub.c[1], 0) != Account.balance)).scalar()
    checks.append({"name": "Each account's ledger movements = its balance", "ledger": mismatched, "books": 0,
                   "count": True})
    for c in checks:
        c["ok"] = c["ledger"] == c["books"]
    return checks
