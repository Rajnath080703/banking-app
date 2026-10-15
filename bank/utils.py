import math
import os
import secrets
import string
from datetime import date, datetime, time, timedelta

from flask import request


TZ_OFFSET = timedelta(minutes=int(os.environ.get("BANK_TZ_OFFSET_MINUTES", "330")))


def local_now():
    return datetime.utcnow() + TZ_OFFSET


def local_today():
    """Today's date in the bank's time zone, independent of the server's own clock setting."""
    return local_now().date()


def to_local(dt):
    return dt + TZ_OFFSET if dt else dt


def gen_number(prefix, length=10):
    # Cryptographic randomness: references must not be guessable from earlier ones.
    return prefix + "".join(secrets.choice(string.digits) for _ in range(length))


def unique_number(model, column, prefix, length=10):
    from .extensions import db
    while True:
        value = gen_number(prefix, length)
        if not db.session.query(model).filter(getattr(model, column) == value).first():
            return value


from .money import emi as calc_emi, fd_maturity  # noqa: E402,F401  (Decimal implementations)


def add_months(d, months):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def date_range_from_request(default="month"):
    """Resolve Today / This Week / This Month / Custom into (start, end, key)."""
    key = request.args.get("range", default)
    today = local_today()
    if key == "today":
        start, end = today, today
    elif key == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif key == "custom":
        start = parse_date(request.args.get("start")) or today.replace(day=1)
        end = parse_date(request.args.get("end")) or today
        if start > end:
            start, end = end, start
    elif key == "year":
        start, end = today.replace(month=1, day=1), today
    elif key == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    else:
        key = "month"
        start, end = today.replace(day=1), today
    return start, end, key


RANGE_LABELS = {"today": "Today", "week": "This Week", "month": "This Month",
                "last_month": "Last Month", "year": "This Year", "custom": "Custom"}


def previous_period(start, end, key):
    """The comparison window for trend percentages ("vs last month" etc.)."""
    if key in ("month", "last_month"):
        prev_end = start - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        # Same number of elapsed days, so a partial month compares fairly.
        span = (end - start).days
        return prev_start, min(prev_start + timedelta(days=span), prev_end)
    if key == "year":
        return start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def compare_label(key):
    return {"today": "vs yesterday", "week": "vs last week", "month": "vs last month",
            "last_month": "vs previous month", "year": "vs last year"}.get(key, "vs previous period")


def pct_change(current, previous):
    current, previous = float(current or 0), float(previous or 0)  # display percentage only
    if not previous:
        return None if not current else 100.0
    return round((current - previous) / previous * 100, 1)


def time_ago(value):
    if not value:
        return ""
    secs = (datetime.utcnow() - value).total_seconds()
    if secs < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hr", 3600), ("min", 60)):
        if secs >= size:
            n = int(secs // size)
            if unit == "day" and n > 30:
                return value.strftime("%d %b %Y")
            return f"{n} {unit}{'s' if n > 1 and unit != 'hr' else ''} ago"
    return ""


def _local_ts(column):
    from sqlalchemy import literal_column
    return column + literal_column(f"interval '{int(TZ_OFFSET.total_seconds() // 60)} minutes'")


def sql_day(column, utc_timestamp=True):
    """'YYYY-MM-DD' local-date bucket for GROUP BY (PostgreSQL)."""
    from sqlalchemy import func, literal_column
    # Inline literal so SELECT and GROUP BY are the identical expression for PostgreSQL.
    return func.to_char(_local_ts(column) if utc_timestamp else column, literal_column("'YYYY-MM-DD'"))


def sql_month(column, utc_timestamp=True):
    """'YYYY-MM' local-month bucket for GROUP BY (PostgreSQL). Pass utc_timestamp=False for DATE columns."""
    from sqlalchemy import func, literal_column
    return func.to_char(_local_ts(column) if utc_timestamp else column, literal_column("'YYYY-MM'"))


def day_bounds(start, end):
    """UTC datetime bounds covering local dates start..end (timestamps are stored in UTC)."""
    return (datetime.combine(start, time.min) - TZ_OFFSET,
            datetime.combine(end, time.max) - TZ_OFFSET)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_float(value, default=None):
    """Parse a user-entered number. Rejects NaN / Infinity, which Python's float() accepts."""
    try:
        v = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def parse_amount(value, minimum="0.01", maximum=None):
    """Money from a form as Decimal (see money.parse_money). None if invalid."""
    from .money import MAX_AMOUNT, D, parse_money
    return parse_money(value, D(minimum), D(maximum) if maximum is not None else MAX_AMOUNT)


def inr(value, decimals=0):
    """Indian digit grouping: 12,34,56,789. Exact for Decimal input (no float conversion)."""
    from decimal import ROUND_HALF_UP, Decimal
    if value is None:
        return "—"
    value = Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    negative = value < 0
    value = abs(value)
    whole = int(value)
    frac = f"{value - whole:.{decimals}f}"[1:] if decimals else ""
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if negative else "") + "₹" + s + frac


def inr_short(value):
    if value is None:
        return "—"
    value = float(value)  # display-only abbreviation (₹1.25 Cr); never used for arithmetic
    v = abs(value)
    sign = "-" if value < 0 else ""
    if v >= 1e7:
        return f"{sign}₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"{sign}₹{v / 1e5:.2f} L"
    if v >= 1e3:
        return f"{sign}₹{v / 1e3:.1f} K"
    return f"{sign}₹{v:.0f}"


STATUS_TONES = {
    "active": "success", "verified": "success", "approved": "success", "paid": "success",
    "resolved": "success", "matured": "info", "closed": "neutral", "inactive": "warning",
    "dormant": "warning", "pending": "warning", "pending review": "warning", "reviewed": "info",
    "assigned": "info", "in progress": "info", "open": "info", "overdue": "danger",
    "rejected": "danger", "expired": "danger", "late": "danger", "critical": "danger",
    "high": "danger", "medium": "warning", "low": "success", "failed": "danger",
    "success": "success", "locked": "danger", "frozen": "info", "reversed": "neutral", "superseded": "neutral",
}


def status_tone(status):
    return STATUS_TONES.get((status or "").lower(), "neutral")
