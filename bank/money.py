"""Money arithmetic for the whole application.

Rules
-----
* Every amount is a ``decimal.Decimal`` — never a binary float.
* Amounts are rounded to the paisa with ROUND_HALF_UP (the convention used on Indian
  bank statements); rates keep 4 decimal places.
* Interest uses an actual/365 day count.

Keeping all of it here means there is exactly one definition of precision and rounding.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

_AMOUNT_RE = re.compile(r"\d{1,13}(\.\d{1,2})?")
PAISA = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
DAYS_IN_YEAR = Decimal(365)
MAX_AMOUNT = Decimal("10000000000.00")  # ₹1,000 crore ceiling for any single amount entered


def D(value) -> Decimal:
    """Convert to Decimal exactly (via str for floats, so 0.1 stays 0.1)."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def q2(value) -> Decimal:
    """Round to the paisa, half-up."""
    return D(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def parse_money(text, minimum: Decimal = PAISA, maximum: Decimal = MAX_AMOUNT) -> Decimal | None:
    """Parse an amount typed by an employee. Returns None for anything that is not a finite
    number within [minimum, maximum], or that has more than two decimal places."""
    if text is None:
        return None
    raw = str(text).replace(",", "").replace("₹", "").strip()
    # Plain digits with at most two decimals: no exponents, signs, NaN/Infinity, or fractions
    # of a paisa (those are typing errors, not something to round silently).
    if not _AMOUNT_RE.fullmatch(raw):
        return None
    value = q2(Decimal(raw))
    if value < minimum or value > maximum:
        return None
    return value


def parse_rate(text, maximum: Decimal = Decimal(100)) -> Decimal | None:
    """Annual interest rate in percent, 0 < rate <= maximum, up to 4 decimals."""
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, TypeError):
        return None
    if not value.is_finite() or value <= 0 or value > maximum:
        return None
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def emi(principal, annual_rate, months: int) -> Decimal:
    """Equated monthly instalment on a reducing balance:
    P·r·(1+r)^n / ((1+r)^n − 1), r = annual_rate / 12 / 100."""
    principal, annual_rate = D(principal), D(annual_rate)
    if months <= 0 or principal <= 0:
        return ZERO
    with localcontext() as ctx:
        ctx.prec = 34
        r = annual_rate / Decimal(1200)
        if r == 0:
            return q2(principal / months)
        growth = (1 + r) ** months
        return q2(principal * r * growth / (growth - 1))


def fd_maturity(principal, annual_rate, months: int) -> Decimal:
    """Term deposit value with quarterly compounding: P·(1 + rate/400)^(months/3)."""
    principal, annual_rate = D(principal), D(annual_rate)
    with localcontext() as ctx:
        ctx.prec = 34
        quarters = Decimal(months) / 3
        return q2(principal * (1 + annual_rate / Decimal(400)) ** quarters)


def simple_interest(principal, annual_rate, days: int) -> Decimal:
    """principal × rate% × days / 365, rounded to the paisa."""
    if days <= 0:
        return ZERO
    return q2(D(principal) * D(annual_rate) / HUNDRED * Decimal(days) / DAYS_IN_YEAR)


def allocate_payment(amount, penalty_due, interest_due, principal_due):
    """Apply a repayment in the standard order: penalty → interest → principal.
    Returns (penalty, interest, principal, excess)."""
    remaining = q2(amount)
    parts = []
    for due in (penalty_due, interest_due, principal_due):
        take = min(remaining, q2(due))
        parts.append(take)
        remaining -= take
    return parts[0], parts[1], parts[2], remaining
