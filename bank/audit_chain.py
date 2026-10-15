"""Tamper-evident audit trail.

Audit rows are written unsealed (fast, no contention). `seal()` then links them in id order
into a SHA-256 hash chain: row_hash = sha256(prev_hash + canonical(row)). A PostgreSQL
trigger rejects any UPDATE of a sealed row and any DELETE/TRUNCATE, and the runtime database
role has no DELETE privilege on audit_log at all. `verify()` recomputes the chain, so an edit
made with superuser access (which can bypass triggers) is still detected.
"""
from __future__ import annotations

import hashlib

from .extensions import db
from .models import AuditLog

GENESIS = "0" * 64
_LOCK_ID = 7_001_001  # advisory lock key: one sealer at a time across all app servers


def _hash(prev: str, row: AuditLog) -> str:
    return hashlib.sha256((prev + "|" + row.canonical()).encode("utf-8")).hexdigest()


def seal(batch: int = 5000) -> int:
    """Seal pending rows. Safe to call from several processes at once. Returns rows sealed."""
    got = db.session.execute(db.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_ID}).scalar()
    if not got:
        return 0
    last = (db.session.query(AuditLog.row_hash).filter(AuditLog.row_hash != None)  # noqa: E711
            .order_by(AuditLog.id.desc()).limit(1).scalar()) or GENESIS
    pending = (AuditLog.query.filter(AuditLog.row_hash == None)  # noqa: E711
               .order_by(AuditLog.id).limit(batch).all())
    for row in pending:
        row.prev_hash = last
        row.row_hash = last = _hash(last, row)
    db.session.commit()
    return len(pending)


def verify(limit: int | None = None):
    """Recompute the chain from the start. Returns dict(ok, checked, broken_at, unsealed)."""
    prev = GENESIS
    checked = 0
    q = AuditLog.query.filter(AuditLog.row_hash != None).order_by(AuditLog.id)  # noqa: E711
    for row in q.yield_per(2000):
        if row.prev_hash != prev or _hash(prev, row) != row.row_hash:
            return {"ok": False, "checked": checked, "broken_at": row.id,
                    "unsealed": AuditLog.query.filter(AuditLog.row_hash == None).count()}  # noqa: E711
        prev = row.row_hash
        checked += 1
        if limit and checked >= limit:
            break
    return {"ok": True, "checked": checked, "broken_at": None,
            "unsealed": AuditLog.query.filter(AuditLog.row_hash == None).count()}  # noqa: E711
