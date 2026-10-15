"""Load ~1M transactions and time the heaviest pages."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ADMIN, DBNAME, M, OWNER_BASE, app, cleanup, client, db

BULK = """
INSERT INTO customer (customer_code, name, branch_id, kyc_status, risk_category, status, created_at, pan_enc, pan_hash, mobile_enc, mobile_hash)
SELECT 'CIFP' || g, 'Perf Customer ' || g, 1, CASE WHEN g % 9 = 0 THEN 'Pending' ELSE 'Verified' END, 'Low', 'Active',
       now() - (g % 900) * interval '1 day', 'x', md5('pan' || g), 'x', md5('mob' || g)
FROM generate_series(1, 50000) g;

INSERT INTO account (account_number, customer_id, branch_id, account_type, balance, interest_rate, status, opened_on, last_txn_on, created_at)
SELECT '9' || lpad(g::text, 11, '0'), c.id, 1, 'Savings', (random() * 500000)::numeric(18,2), 3, 'Active',
       current_date - (g % 900), current_date - (g % 30), now()
FROM generate_series(1, 60000) g JOIN customer c ON c.customer_code = 'CIFP' || (1 + g % 50000);

INSERT INTO transaction (txn_ref, account_id, customer_id, branch_id, txn_type, channel, category, amount, balance_after, description, flagged, timestamp, status)
SELECT 'TXP' || g, a.id, a.customer_id, 1, CASE WHEN g % 2 = 0 THEN 'Credit' ELSE 'Debit' END, 'UPI', 'Deposit',
       (random() * 50000)::numeric(18,2), 0, 'perf', false, now() - (g % 400) * interval '1 day' - (g % 86400) * interval '1 second', 'Posted'
FROM generate_series(1, 1000000) g JOIN account a ON a.account_number = '9' || lpad((1 + g % 60000)::text, 11, '0');

INSERT INTO loan (loan_number, customer_id, account_id, branch_id, loan_type, amount, outstanding, interest_rate, tenure_months, emi,
                  disbursed_on, next_due_date, overdue_days, status, created_at)
SELECT 'LNP' || g, a.customer_id, a.id, 1, (ARRAY['gold','personal','home','vehicle','business','education','other'])[1 + g % 7],
       100000, 60000, 11, 24, 4700, current_date - (g % 600), current_date + (g % 40) - 10, 0,
       (ARRAY['Active','Active','Active','Closed','Overdue','Pending'])[1 + g % 6], now() - (g % 600) * interval '1 day'
FROM generate_series(1, 30000) g JOIN account a ON a.account_number = '9' || lpad((1 + g % 60000)::text, 11, '0');
ANALYZE;
"""

try:
    t0 = time.time()
    # Bulk load over the owner connection, like a data migration: the app's own role has a
    # statement timeout (a deliberate safety limit) that a million-row INSERT would exceed.
    from sqlalchemy import create_engine
    owner = create_engine(f"{OWNER_BASE}/{DBNAME}")
    with owner.begin() as conn:
        conn.execute(db.text(BULK))
    owner.dispose()
    with app.app_context():
        counts = {m.__tablename__: m.query.count() for m in (M.Customer, M.Account, M.Transaction, M.Loan)}
    print(f"loaded {counts} in {time.time() - t0:.0f}s")
    admin = client(*ADMIN)
    pages = ["/?range=month", "/?range=year", "/transactions/?range=month", "/customers/?q=Perf%20Customer%204999",
             "/customers/?q=CIFP777", "/accounts/?sort=balance", "/loans/", "/loans/?status=Overdue",
             "/kyc/", "/reports/daily-transactions?range=month", "/reports/loans?range=year", "/reports/overdue-loans?range=year", "/reports/kyc-status?range=year", "/reports/loans?range=year&export=xlsx", "/alerts/",
             "/accounts/new?find=Perf%20Customer%2012345", "/accounts/new?find=CIFP4242", "/approvals/",
             "/transactions/transfer?from=900000000001&to=900000000002", "/accounts/?status=Frozen"]
    admin.get("/")  # warm-up (also runs the hourly scan once)
    for p in pages:
        best = None
        for _ in range(2):
            s = time.perf_counter()
            r = admin.get(p)
            el = time.perf_counter() - s
            best = el if best is None else min(best, el)
        print(f"  {r.status_code}  {best * 1000:7.0f} ms  {p}")
    s = time.perf_counter()
    with app.app_context():
        from bank.services import scan_scheduled_alerts
        scan_scheduled_alerts(force=True)
    print(f"  hourly background scan over 30k loans: {(time.perf_counter() - s) * 1000:.0f} ms")
finally:
    cleanup()
