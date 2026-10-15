# Audit test suite

Each script builds a throwaway PostgreSQL database inside the bank-postgres container, runs real
workflows through the web app, and drops it afterwards. Your real bankdb database is never touched.

Run from the project folder with Docker running:

| Script | What it checks |
|---|---|
| python tests/test_banking_audit.py | 26 money / security regressions from the audit (prints CONFIRMED if any come back) |
| python tests/test_permissions.py | every role x every page, server-side, against the role's granted rights |
| python tests/test_end_to_end.py | full branch workflow on an empty database, all pages and exports |
| python tests/perf_million_rows.py | loads 1M transactions and times the heaviest pages (about 5 minutes) |
| python tests/test_dashboard_numbers.py | dashboard KPI cards vs independent SQL totals |
