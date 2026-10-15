"""Database-level guarantees, tested as the restricted runtime role `bank_app`:
migrations apply cleanly, unbalanced journals are refused, history is append-only,
sealed audit rows cannot change, CHECK constraints hold, and the role cannot alter the schema."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

env = {**dotenv_values(os.path.join(ROOT, ".env")), **{k: v for k, v in os.environ.items() if k.startswith(("BANK_", "DATABASE_URL", "POSTGRES_"))}}
DB = "bankdb_integrity"
owner_base = env["BANK_DB_OWNER_URL"].rsplit("/", 1)[0]
app_base = env["DATABASE_URL"].rsplit("/", 1)[0]


def psql(sql):
    """Admin statements (CREATE/DROP DATABASE) over a normal owner connection — works locally and in CI."""
    from sqlalchemy import create_engine, text as _text
    eng = create_engine(env["BANK_DB_OWNER_URL"].rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(_text(sql))
    eng.dispose()


results = []


def expect_error(label, engine, sql, params=None, needle=""):
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
        results.append((label, False, "statement succeeded"))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0]
        results.append((label, needle.lower() in msg.lower(), msg[:110]))


psql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
psql(f"CREATE DATABASE {DB}")
try:
    os.environ["DATABASE_URL"] = f"{app_base}/{DB}"
    from bank.bootstrap import prepare_database  # noqa: E402
    prepare_database(owner_url=f"{owner_base}/{DB}", app_password=env["BANK_APP_DB_PASSWORD"])
    app = create_engine(f"{app_base}/{DB}")
    c = app
    if True:
        with app.connect() as cx:
            version = cx.execute(text("select version_num from alembic_version")).scalar()
            branch = cx.execute(text("select id from branch limit 1")).scalar()
        results.append(("Migrations applied to head", bool(version), version))
        # Unbalanced journal entry is refused at commit (deferred trigger)
        expect_error("Unbalanced journal entry refused", c, """
            WITH e AS (INSERT INTO journal_entry (entry_ref, posted_at, value_date, branch_id, narration)
                       VALUES ('JETEST1', now(), current_date, :b, 'test') RETURNING id)
            INSERT INTO journal_line (entry_id, gl_code, branch_id, debit, credit)
            SELECT id, '1000', :b, 100, 0 FROM e""", {"b": branch}, needle="unbalanced")
        # Balanced entry is accepted
        with app.begin() as cx:
            eid = cx.execute(text("""INSERT INTO journal_entry (entry_ref, posted_at, value_date, branch_id, narration)
                                    VALUES ('JETEST2', now(), current_date, :b, 'balanced') RETURNING id"""), {"b": branch}).scalar()
            cx.execute(text("INSERT INTO journal_line (entry_id, gl_code, branch_id, debit, credit) VALUES "
                           "(:e,'1000',:b,250.50,0), (:e,'2000',:b,0,250.50)"), {"e": eid, "b": branch})
        results.append(("Balanced journal entry accepted", True, f"entry {eid}"))
        expect_error("Journal line cannot be edited", c, "UPDATE journal_line SET debit = 1 WHERE entry_id = :e", {"e": eid}, "permission denied")
        expect_error("Journal entry cannot be deleted", c, "DELETE FROM journal_entry WHERE id = :e", {"e": eid}, "permission denied")
        # Audit log: insert ok, seal ok once, then immutable; no delete
        with app.begin() as cx:
            aid = cx.execute(text("INSERT INTO audit_log (action, result, timestamp) VALUES ('Test', 'success', now()) RETURNING id")).scalar()
            cx.execute(text("UPDATE audit_log SET prev_hash = repeat('0',64), row_hash = repeat('a',64) WHERE id = :a"), {"a": aid})
        results.append(("Audit row can be sealed once", True, f"row {aid}"))
        expect_error("Sealed audit row cannot be re-sealed", c, "UPDATE audit_log SET row_hash = repeat('b',64) WHERE id = :a", {"a": aid}, "sealed")
        expect_error("Audit row content cannot be edited", c, "UPDATE audit_log SET action = 'Changed' WHERE id = :a", {"a": aid}, "permission denied")
        expect_error("Audit row cannot be deleted", c, "DELETE FROM audit_log WHERE id = :a", {"a": aid}, "permission denied")
        expect_error("Audit log cannot be truncated", c, "TRUNCATE audit_log", needle="permission denied")
        # CHECK constraints
        with app.begin() as cx:
            cust = cx.execute(text("""INSERT INTO customer (customer_code, name, branch_id, kyc_status, risk_category, status, created_at)
                                 VALUES ('CIFT1','Check Test',:b,'Pending','Low','Active',now()) RETURNING id"""), {"b": branch}).scalar()
        expect_error("Negative account balance refused", c, """INSERT INTO account (account_number, customer_id, branch_id, account_type, balance,
                     interest_rate, status, created_at) VALUES ('500000000001', :c, :b, 'Savings', -1, 3, 'Active', now())""",
                     {"c": cust, "b": branch}, "ck_account_balance_non_negative")
        expect_error("Unknown account status refused", c, """INSERT INTO account (account_number, customer_id, branch_id, account_type, balance,
                     interest_rate, status, created_at) VALUES ('500000000002', :c, :b, 'Savings', 0, 3, 'Hacked', now())""",
                     {"c": cust, "b": branch}, "ck_account_status")
        expect_error("Loan rate above 36% refused", c, """INSERT INTO loan (loan_number, customer_id, branch_id, loan_type, amount, outstanding,
                     interest_rate, tenure_months, emi, status, created_at) VALUES ('LNT1', :c, :b, 'personal', 1000, 0, 500, 12, 0, 'Pending', now())""",
                     {"c": cust, "b": branch}, "ck_loan_rate_range")
        # Schema changes are impossible for the app role
        expect_error("App role cannot drop tables", c, "DROP TABLE audit_log", needle="must be owner")
        expect_error("App role cannot disable triggers", c, "ALTER TABLE audit_log DISABLE TRIGGER ALL", needle="must be owner")
    app.dispose()
finally:
    psql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")

bad = [r for r in results if not r[1]]
for label, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {label} :: {detail}")
print(f"db integrity: {len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
