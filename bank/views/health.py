"""Liveness / readiness probes for load balancers and orchestrators. They reveal nothing beyond
pass/fail per dependency and need no login."""
import os

from flask import Blueprint, jsonify

from ..extensions import cache, db, limiter

bp = Blueprint("health", __name__)


@bp.route("/healthz")
@limiter.exempt
def liveness():
    return jsonify(status="ok")


@bp.route("/readyz")
@limiter.exempt
def readiness():
    checks = {}
    try:
        db.session.execute(db.text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        db.session.rollback()
        checks["database"] = "unavailable"
    try:
        from alembic.script import ScriptDirectory
        from flask import current_app
        script = ScriptDirectory.from_config(current_app.extensions["migrate"].migrate.get_config())
        current = db.session.execute(db.text("SELECT version_num FROM alembic_version")).scalar()
        checks["schema"] = "ok" if current == script.get_current_head() else "outdated"
    except Exception:  # noqa: BLE001
        db.session.rollback()
        checks["schema"] = "unknown"
    if os.environ.get("REDIS_URL"):
        try:
            cache.set("readyz", "1", timeout=5)
            checks["cache"] = "ok" if cache.get("readyz") == "1" else "unavailable"
        except Exception:  # noqa: BLE001
            checks["cache"] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return jsonify(status="ready" if ok else "not ready", checks=checks), (200 if ok else 503)
