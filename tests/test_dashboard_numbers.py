"""Dashboard KPI cards vs independent SQL over the same data (Today range)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import cleanup, db, deposit, post, setup_bank, app

try:
    S, cid, aid, acc_no = setup_bank()
    off = S["officer"]
    for amt in (1000, 2500.50):
        deposit(off, acc_no, amt)
    deposit(off, acc_no, 300.25, "Debit")
    post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "account_id": aid, "amount": "50000",
                             "interest_rate": "12", "tenure_months": "12"}, f"/loans/new?customer={cid}")
    with app.test_request_context("/?range=today"):
        from bank.utils import day_bounds, local_today
        from bank.views.dashboard import compute_kpis
        today = local_today()
        k = compute_kpis([1], today, today)
        s, e = day_bounds(today, today)
        sql = lambda t: db.session.execute(db.text(t), {"s": s, "e": e}).scalar() or 0  # noqa: E731
        expected = {
            "income": sql("select sum(amount) from transaction where txn_type='Credit' and timestamp between :s and :e"),
            "outgoing": sql("select sum(amount) from transaction where txn_type='Debit' and timestamp between :s and :e"),
            "total_customers": sql("select count(*) from customer"),
            "new_accounts": sql("select count(*) from account where opened_on = current_date"),
            "pending_approvals": sql("select count(*) from approval_request where status in ('Pending Review','Reviewed')"),
            "kyc_pending": sql("select count(*) from customer where kyc_status='Pending'"),
            "casa": sql("select sum(balance) from account where status <> 'Closed'"),
        }
    # Trend vs the previous period: everything happened today, so yesterday's deposits were zero
    # (a pending FD's funding debit must not make "yesterday" negative).
    post(off, "/deposits/new", {"customer": cid, "account_id": aid, "principal": "1000", "tenure_months": "6"},
         f"/deposits/new?customer={cid}")
    with app.test_request_context("/?range=today"):
        from bank.views.dashboard import compute_kpis, compute_trends
        k2 = compute_kpis([1], today, today)
        trend = compute_trends([1], today, today, "today", k2)["total_deposits"]
    expected_trend_ok = trend == 100.0
    print("  deposits trend vs previous day:", trend, "(expected 100.0)")
    bad = {key: (k[key], float(v)) for key, v in expected.items() if abs(float(k[key]) - float(v)) > 0.005}
    if not expected_trend_ok:
        bad["total_deposits_trend"] = (trend, 100.0)
    print("dashboard KPIs checked:", len(expected), "| mismatches:", bad or "none")
    print("  income", k["income"], "outgoing", k["outgoing"], "pending approvals", k["pending_approvals"])
    html = S["admin"].get("/?range=today").get_data(as_text=True)
    print("  dashboard page renders:", "Pending Approvals" in html)
finally:
    cleanup()
sys.exit(1 if bad else 0)
