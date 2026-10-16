"""Database preparation: runtime role, migrations, reference data.

Two PostgreSQL roles are used:
  * the *owner* (BANK_DB_OWNER_URL) — owns the schema; used only to run migrations;
  * `bank_app` (DATABASE_URL)       — what the web app connects as. It cannot change the
    schema, cannot delete or rewrite audit / ledger / repayment history, and can only seal
    (not edit) audit rows. See migration 61305e4a6b8d.
"""
from __future__ import annotations

import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

APP_ROLE = "bank_app"


def _normalise(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def ensure_app_role(owner_url: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", password or ""):
        raise RuntimeError("BANK_APP_DB_PASSWORD must be 16-128 letters, digits, '-' or '_'.")
    engine = create_engine(_normalise(owner_url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).scalar()
        verb = "ALTER" if exists else "CREATE"
        # password validated above (no quotes possible), roles cannot be bound parameters
        conn.execute(text(f"{verb} ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                          f"NOINHERIT PASSWORD '{password}'"))
        dbname = make_url(_normalise(owner_url)).database
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{dbname}" TO {APP_ROLE}'))
    engine.dispose()


def prepare_database(owner_url: str | None = None, app_password: str | None = None) -> None:
    """Create/refresh the runtime role, apply all migrations as the owner, seed reference data."""
    from flask_migrate import upgrade

    from . import create_app
    from .extensions import db
    from .seed import ensure_core

    owner_url = owner_url or os.environ["BANK_DB_OWNER_URL"]
    app_password = app_password or os.environ["BANK_APP_DB_PASSWORD"]
    ensure_app_role(owner_url, app_password)
    owner_app = create_app({"SQLALCHEMY_DATABASE_URI": _normalise(owner_url)})
    with owner_app.app_context():
        upgrade()
        # Grants for tables created by migrations that ran before the role existed.
        db.session.execute(text("""
            GRANT USAGE ON SCHEMA public TO bank_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bank_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bank_app;
            REVOKE UPDATE, DELETE, TRUNCATE ON journal_entry, journal_line, loan_repayment,
                activity, login_history, document_verification, audit_log, transaction, interest_tds FROM bank_app;
            GRANT UPDATE (txn_id) ON interest_tds TO bank_app;
            GRANT UPDATE (prev_hash, row_hash) ON audit_log TO bank_app;
            GRANT UPDATE (flagged, journal_entry_id, status) ON transaction TO bank_app;
            REVOKE ALL ON alembic_version FROM bank_app;
            GRANT SELECT ON alembic_version TO bank_app;
        """))
        db.session.commit()
        ensure_core()
        db.session.remove()
        db.engine.dispose()
