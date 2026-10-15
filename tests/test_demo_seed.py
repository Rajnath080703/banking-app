"""`flask seed-demo` (dev sample data): refused unless explicitly allowed, refused on a database
that already has customers, and when allowed it loads data through the real workflows."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import M, app, cleanup, q  # noqa: E402

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def run_seed(**env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return app.test_cli_runner().invoke(args=["seed-demo"])
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


try:
    r = run_seed(BANK_DEMO_DATA_ALLOWED="0", BANK_DEMO_STAFF_PASSWORD="Demo#Strong2026x")
    check("Refused where BANK_DEMO_DATA_ALLOWED is not 1 (main)", r.exit_code != 0 and "Refused" in r.output
          and q(lambda: M.Customer.query.count()) == 0 and q(lambda: M.Employee.query.count()) == 1, r.output.strip()[:90])
    r = run_seed(BANK_DEMO_DATA_ALLOWED="1", BANK_DEMO_STAFF_PASSWORD="weak")
    check("Refused with a weak sample-staff password", r.exit_code != 0 and q(lambda: M.Employee.query.count()) == 1, r.output.strip()[:90])
    r = run_seed(BANK_DEMO_DATA_ALLOWED="1", BANK_DEMO_STAFF_PASSWORD="Demo#Strong2026x")
    counts = q(lambda: (M.Customer.query.count(), M.Account.query.count(), M.Loan.query.count(),
                        M.ApprovalRequest.query.filter_by(status="Pending Review").count()))
    check("Allowed (dev): customers, accounts, loans and a pending approval are created", r.exit_code == 0 and counts[0] == 5
          and counts[1] == 4 and counts[2] == 3 and counts[3] >= 1, f"{counts} | {r.output.strip()[-80:]}")
    with app.app_context():
        from bank.ledger import reconciliation
        check("Books reconcile after loading", all(c["ok"] for c in reconciliation()))
        kyc = {c.name: c.kyc_status for c in M.Customer.query.all()}
    check("Data went through the real KYC rules (one customer left with KYC pending)", kyc.get("Fatima Begum") == "Pending"
          and kyc.get("Priya Raman") == "Verified", kyc)
    r = run_seed(BANK_DEMO_DATA_ALLOWED="1", BANK_DEMO_STAFF_PASSWORD="Demo#Strong2026x")
    check("A second run is refused (database already has customers)", r.exit_code != 0 and "already has customers" in r.output
          and q(lambda: M.Customer.query.count()) == 5)
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\ndemo seed: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
