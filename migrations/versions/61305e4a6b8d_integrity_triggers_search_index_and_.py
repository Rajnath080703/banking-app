"""integrity triggers, search index and least-privilege grants

* journal entries must balance (deferred constraint trigger, checked at COMMIT)
* sealed audit rows cannot be changed; audit rows can never be deleted or truncated
* ledger and repayment history is append-only
* trigram index for partial customer-name search
* privileges for the runtime role `bank_app` (created by bootstrap.prepare_database)

Revision ID: 61305e4a6b8d
Revises: e9dae407cd6e
Create Date: 2026-10-08 15:58:12.338630
"""
from alembic import op

revision = "61305e4a6b8d"
down_revision = "e9dae407cd6e"
branch_labels = None
depends_on = None

APPEND_ONLY = ("journal_entry", "journal_line", "loan_repayment", "audit_log", "activity",
               "login_history", "document_verification")


def upgrade():
    # ---- ledger: every entry balances -------------------------------------------------
    op.execute("""
    CREATE OR REPLACE FUNCTION bank_check_entry_balanced() RETURNS trigger AS $$
    DECLARE dr numeric; cr numeric;
    BEGIN
        SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0) INTO dr, cr
          FROM journal_line WHERE entry_id = NEW.entry_id;
        IF dr <> cr THEN
            RAISE EXCEPTION 'journal entry % is unbalanced: debits % <> credits %', NEW.entry_id, dr, cr
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER journal_line_balanced
        AFTER INSERT OR UPDATE ON journal_line
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION bank_check_entry_balanced();
    """)

    # ---- append-only history ----------------------------------------------------------
    op.execute("""
    CREATE OR REPLACE FUNCTION bank_forbid_change() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION '% on % is not allowed: this history is append-only', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """)
    for table in ("journal_entry", "journal_line", "loan_repayment"):
        op.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION bank_forbid_change();")
    for table in APPEND_ONLY:
        op.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION bank_forbid_change();")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION bank_forbid_change();")

    # ---- audit log: only the one-time sealing update is allowed -----------------------
    op.execute("""
    CREATE OR REPLACE FUNCTION bank_audit_seal_only() RETURNS trigger AS $$
    BEGIN
        IF OLD.row_hash IS NOT NULL THEN
            RAISE EXCEPTION 'audit row % is sealed and cannot be modified', OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF (NEW.id, NEW.employee_id, NEW.email, NEW.action, NEW.module, NEW.reference, NEW.details,
            NEW.ip_address, NEW.result, NEW.timestamp)
           IS DISTINCT FROM
           (OLD.id, OLD.employee_id, OLD.email, OLD.action, OLD.module, OLD.reference, OLD.details,
            OLD.ip_address, OLD.result, OLD.timestamp) THEN
            RAISE EXCEPTION 'audit row % content cannot be modified', OLD.id
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER audit_log_seal_only BEFORE UPDATE ON audit_log "
               "FOR EACH ROW EXECUTE FUNCTION bank_audit_seal_only();")

    # ---- fast partial name search ------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute("CREATE INDEX IF NOT EXISTS ix_customer_name_trgm ON customer USING gin (name gin_trgm_ops);")

    # ---- least-privilege runtime role ---------------------------------------------------
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bank_app') THEN
            GRANT USAGE ON SCHEMA public TO bank_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bank_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bank_app;
            -- history tables: append-only for the application
            REVOKE UPDATE, DELETE, TRUNCATE ON journal_entry, journal_line, loan_repayment,
                activity, login_history, document_verification FROM bank_app;
            REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM bank_app;
            GRANT UPDATE (prev_hash, row_hash) ON audit_log TO bank_app;
            -- posted transactions: only the risk flag / ledger link / status may change
            REVOKE UPDATE, DELETE, TRUNCATE ON transaction FROM bank_app;
            GRANT UPDATE (flagged, journal_entry_id, status) ON transaction TO bank_app;
            REVOKE ALL ON alembic_version FROM bank_app;
            GRANT SELECT ON alembic_version TO bank_app;
        END IF;
    END $$;
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_customer_name_trgm;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_seal_only ON audit_log;")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};")
    for table in ("journal_entry", "journal_line", "loan_repayment"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table};")
    op.execute("DROP TRIGGER IF EXISTS journal_line_balanced ON journal_line;")
    op.execute("DROP FUNCTION IF EXISTS bank_audit_seal_only();")
    op.execute("DROP FUNCTION IF EXISTS bank_forbid_change();")
    op.execute("DROP FUNCTION IF EXISTS bank_check_entry_balanced();")
