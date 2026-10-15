"""Profile the scheduled scan and the loans report on a medium data set (N loans).

    BANK_TEST_DB=bankdb_prof python tests/perf_scan.py [N]
"""
import cProfile
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ADMIN, DBNAME, M, OWNER_BASE, app, cleanup, client, db  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
BULK = f"""
INSERT INTO customer (customer_code, name, branch_id, kyc_status, risk_category, status, created_at, pan_enc, pan_hash, mobile_enc, mobile_hash)
SELECT 'CIFS' || g, 'Scan Customer ' || g, 1, 'Verified', 'Low', 'Active', now(), 'x', md5('pan' || g), 'x', md5('mob' || g)
FROM generate_series(1, {N}) g;
INSERT INTO account (account_number, customer_id, branch_id, account_type, balance, interest_rate, status, opened_on, last_txn_on, created_at)
SELECT '8' || lpad(g::text, 11, '0'), c.id, 1, 'Savings', 1000, 3, 'Active', current_date, current_date, now()
FROM generate_series(1, {N}) g JOIN customer c ON c.customer_code = 'CIFS' || g;
INSERT INTO loan (loan_number, customer_id, account_id, branch_id, loan_type, amount, outstanding, interest_rate, tenure_months, emi,
                  disbursed_on, next_due_date, overdue_days, status, created_at)
SELECT 'LNS' || g, a.customer_id, a.id, 1, 'personal', 100000, 60000, 11, 24, 4700, current_date - 100,
       current_date + (g % 40) - 10, 0, 'Active', now()
FROM generate_series(1, {N}) g JOIN account a ON a.account_number = '8' || lpad(g::text, 11, '0');
ANALYZE;
"""

try:
    from sqlalchemy import create_engine
    owner = create_engine(f"{OWNER_BASE}/{DBNAME}")
    with owner.begin() as conn:
        conn.execute(db.text(BULK))
    owner.dispose()
    with app.app_context():
        from bank.services import scan_scheduled_alerts
        prof = cProfile.Profile()
        t = time.perf_counter()
        prof.enable()
        scan_scheduled_alerts(force=True)
        prof.disable()
        first = time.perf_counter() - t
        alerts = M.Alert.query.count()
        t = time.perf_counter()
        scan_scheduled_alerts(force=True)
        second = time.perf_counter() - t
        db.session.remove()
    print(f"loans {N}: first scan {first:.1f}s creating {alerts} alerts; repeat scan {second:.1f}s")
    pstats.Stats(prof).sort_stats("cumulative").print_stats(15)
    admin = client(*ADMIN)
    for url in ("/reports/loans?range=year", "/reports/loans?range=year&export=xlsx"):
        t = time.perf_counter()
        r = admin.get(url)
        print(f"  {r.status_code} {time.perf_counter() - t:6.2f}s {url}")
finally:
    cleanup()
