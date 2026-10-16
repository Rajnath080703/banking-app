"""Run every test suite with coverage and print one summary. Exit code 0 only if all pass.

    python tests/run_all.py            # all suites except the 1M-row performance run
    python tests/run_all.py --perf     # include tests/perf_million_rows.py (several minutes)
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITES = [
    ("Database integrity (constraints, triggers, privileges)", "test_db_integrity.py", "bankdb_t_integrity"),
    ("Regression: 26 audit defects", "test_banking_audit.py", "bankdb_t_audit"),
    ("Hardening: ledger, calculations, security", "test_hardening.py", "bankdb_t_hard"),
    ("Permission sweep: every role × page × action", "test_permissions.py", "bankdb_t_perm"),
    ("End-to-end employee journey", "test_end_to_end.py", "bankdb_t_e2e"),
    ("Dashboard figures vs SQL", "test_dashboard_numbers.py", "bankdb_t_dash"),
    ("Workflows: tickets, alerts, scan, rejections, admin", "test_workflows.py", "bankdb_t_wf"),
    ("Employee workflow improvements", "test_ux_workflows.py", "bankdb_t_ux"),
    ("Security & branch isolation", "test_security_isolation.py", "bankdb_t_sec"),
    ("Operations: open account, transfers, reversals", "test_operations.py", "bankdb_t_ops"),
    ("Account opening (both entry points)", "test_open_account.py", "bankdb_t_open"),
    ("Dev sample-data loader", "test_demo_seed.py", "bankdb_t_demo"),
    ("Receipts & private search", "test_receipts_search.py", "bankdb_t_rcpt"),
    ("Rate limits", "test_rate_limits.py", "bankdb_t_rl"),
]
if "--perf" in sys.argv:
    SUITES.append(("Performance at 1M transactions", "perf_million_rows.py", "bankdb_t_perf"))

env = dict(os.environ, PYTHONIOENCODING="utf-8", COVERAGE_FILE=os.path.join(ROOT, ".coverage"))
subprocess.run([sys.executable, "-m", "coverage", "erase"], cwd=ROOT, env=env)
summary = []
for title, script, db in SUITES:
    start = time.time()
    r = subprocess.run([sys.executable, "-m", "coverage", "run", "-p", "--source=bank", os.path.join("tests", script)],
                       cwd=ROOT, env=dict(env, BANK_TEST_DB=db), capture_output=True, text=True, encoding="utf-8")
    # the app's JSON request log also goes to stdout; the suite's own summary is the last plain line
    tail = [line for line in r.stdout.splitlines() if line.strip() and not line.lstrip().startswith("{")][-1:] or ["(no output)"]
    summary.append((r.returncode == 0, title, tail[0], time.time() - start))
    print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {title}  —  {tail[0]}  ({time.time() - start:.0f}s)", flush=True)
    if r.returncode != 0:
        print("\n".join(r.stdout.splitlines()[-25:]))
        print("\n".join(r.stderr.splitlines()[-25:]))
subprocess.run([sys.executable, "-m", "coverage", "combine", "-q"], cwd=ROOT, env=env)
cov = subprocess.run([sys.executable, "-m", "coverage", "report", "--skip-covered", "--sort=cover"], cwd=ROOT, env=env,
                     capture_output=True, text=True)
print("\n" + "\n".join(cov.stdout.splitlines()[-25:]))
ok = all(s[0] for s in summary)
print(f"\n{sum(1 for s in summary if s[0])}/{len(summary)} suites passed")
sys.exit(0 if ok else 1)
