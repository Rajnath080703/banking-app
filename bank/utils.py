import random
import string
from datetime import date, datetime, time, timedelta

from flask import request


def gen_number(prefix, length=10):
    return prefix + "".join(random.choices(string.digits, k=length))


def unique_number(model, column, prefix, length=10):
    from .extensions import db
    while True:
        value = gen_number(prefix, length)
        if not db.session.query(model).filter(getattr(model, column) == value).first():
            return value


def calc_emi(principal, annual_rate, months):
    if months <= 0:
        return 0
    r = annual_rate / 12 / 100
    if r == 0:
        return round(principal / months, 2)
    return round(principal * r * (1 + r) ** months / ((1 + r) ** months - 1), 2)


def fd_maturity(principal, annual_rate, months):
    # Quarterly compounding, as commonly used for term deposits.
    return round(principal * (1 + annual_rate / 400) ** (months / 3), 2)


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
    today = date.today()
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


def day_bounds(start, end):
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_float(value, default=None):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def inr(value, decimals=0):
    """Indian digit grouping: 12,34,56,789."""
    if value is None:
        return "—"
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
    "success": "success", "locked": "danger",
}


def status_tone(status):
    return STATUS_TONES.get((status or "").lower(), "neutral")
