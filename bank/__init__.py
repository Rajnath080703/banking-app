import os
import secrets
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Load .env before anything reads configuration (seed defaults, keys, database URL).
# Real environment variables always win over .env (production secrets come from the platform).
load_dotenv(os.path.join(ROOT, ".env"))

from .crypto import init_crypto  # noqa: E402
from .extensions import cache, db, limiter, login_manager, migrate  # noqa: E402
from .logging_setup import configure_logging  # noqa: E402

PRODUCTION = os.environ.get("BANK_ENV", "development").lower() == "production"


def _secret_key(app):
    key = os.environ.get("BANK_SECRET_KEY")
    if key:
        return key
    if PRODUCTION:
        raise RuntimeError("BANK_SECRET_KEY must be set in production.")
    path = os.path.join(app.instance_path, "secret.key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    return key


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to the .env file, e.g. "
            "DATABASE_URL=postgresql+psycopg://bank_app:<password>@127.0.0.1:5432/bankdb")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must point to a PostgreSQL database (postgresql+psycopg://...).")
    return url


def _production_checks(app):
    problems = []
    if not app.config["SESSION_COOKIE_SECURE"]:
        problems.append("BANK_HTTPS=1 (secure cookies + HSTS)")
    if not os.environ.get("BANK_DATA_KEY"):
        problems.append("BANK_DATA_KEY from the secret store")
    if app.config["RATELIMIT_STORAGE_URI"].startswith("memory"):
        problems.append("REDIS_URL (shared rate limits across servers)")
    if "bank_app" not in app.config["SQLALCHEMY_DATABASE_URI"]:
        problems.append("DATABASE_URL using the restricted bank_app role")
    if problems:
        raise RuntimeError("Refusing to start in production without: " + "; ".join(problems))


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.join(app.instance_path, "uploads"), exist_ok=True)

    https = os.environ.get("BANK_HTTPS", "0") == "1"
    redis_url = os.environ.get("REDIS_URL", "").strip()
    app.config.update(
        SECRET_KEY=_secret_key(app),
        SQLALCHEMY_DATABASE_URI=_database_url(),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 1800,
                                   "pool_size": int(os.environ.get("BANK_DB_POOL_SIZE", "10")),
                                   "max_overflow": int(os.environ.get("BANK_DB_MAX_OVERFLOW", "10")),
                                   "connect_args": {"connect_timeout": 5,
                                                    "options": "-c statement_timeout=30000"}},
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOAD_FOLDER=os.environ.get("BANK_UPLOAD_DIR") or os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_NAME="__Host-bank_session" if https else "bank_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=https,
        REMEMBER_COOKIE_DURATION=timedelta(seconds=0),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        RATELIMIT_STORAGE_URI=redis_url or "memory://",
        RATELIMIT_HEADERS_ENABLED=True,
        RATELIMIT_DEFAULT="600 per minute",
        CACHE_TYPE="RedisCache" if redis_url else "SimpleCache",
        CACHE_REDIS_URL=redis_url or None,
        CACHE_DEFAULT_TIMEOUT=60,
    )
    if test_config:
        app.config.update(test_config)

    trusted = int(os.environ.get("BANK_TRUSTED_PROXIES", "0"))
    if trusted > 0:
        # Honour X-Forwarded-* only from the given number of trusted reverse proxies.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted, x_proto=trusted, x_host=trusted)

    configure_logging(app)
    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(ROOT, "migrations"))
    login_manager.init_app(app)
    limiter.init_app(app)
    cache.init_app(app)
    init_crypto(app)
    if PRODUCTION and not app.config.get("TESTING"):
        _production_checks(app)
    if os.environ.get("SENTRY_DSN"):
        try:  # optional error tracking; only active when a DSN is configured
            import sentry_sdk
            sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], send_default_pii=False, traces_sample_rate=0.0)
        except ImportError:
            app.logger.warning("SENTRY_DSN set but sentry-sdk is not installed")

    from . import models
    from .security import audit, csrf_token, form_nonce, verify_csrf
    from .utils import RANGE_LABELS, inr, inr_short, status_tone, time_ago

    @login_manager.user_loader
    def load_user(user_id):
        try:
            emp_id, token = user_id.split(":", 1)
            emp = db.session.get(models.Employee, int(emp_id))
        except (ValueError, TypeError):
            return None
        if emp and emp.is_active and (emp.session_token or "") == token:
            return emp
        return None

    @app.before_request
    def request_context():
        rid = request.headers.get("X-Request-ID", "")
        g.request_id = rid if (8 <= len(rid) <= 64 and rid.replace("-", "").isalnum()) else uuid.uuid4().hex
        g.started = time.perf_counter()

    @app.before_request
    def security_gate():
        if request.endpoint in ("health.liveness", "health.readiness"):
            return None
        verify_csrf()
        if not current_user.is_authenticated or request.endpoint in (None, "static"):
            return None
        now = datetime.utcnow().timestamp()
        # Absolute lifetime: a session ends after N hours even if it is in constant use.
        login_at = session.get("login_at")
        absolute = models.Setting.get_int("absolute_session_hours") * 3600
        timeout = models.Setting.get_int("session_timeout_minutes") * 60
        last = session.get("last_seen")
        reason = None
        if not login_at or now - login_at > absolute:
            reason = "Session expired (maximum length)"
        elif last and now - last > timeout:
            reason = "Session timeout"
        if reason:
            audit(reason, "auth")
            db.session.commit()
            logout_user()
            session.clear()
            flash("Your session expired. Please sign in again.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        session["last_seen"] = now
        session.permanent = True
        from .services import start_background_scan
        start_background_scan(app)
        allowed = {"auth.logout", "settings.change_password"}
        if request.endpoint not in allowed:
            expiry_days = models.Setting.get_int("password_expiry_days")
            expired = (current_user.password_changed_at and expiry_days and
                       datetime.utcnow() - current_user.password_changed_at > timedelta(days=expiry_days))
            if current_user.must_change_password or expired:
                flash("Please set a new password to continue.", "warning")
                return redirect(url_for("settings.change_password"))
            if (models.Setting.get("enforce_mfa") == "1" and not current_user.mfa_enabled
                    and request.endpoint not in {"settings.mfa_setup", "settings.mfa_enable"}):
                flash("Your bank requires multi-factor authentication. Please set it up.", "warning")
                return redirect(url_for("settings.mfa_setup"))
        return None

    from . import private_search
    app.before_request(private_search.redirect_posted_search)  # after security_gate's CSRF check

    @app.after_request
    def security_headers(resp):
        h = resp.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["X-Permitted-Cross-Domain-Policies"] = "none"
        h["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if app.config["SESSION_COOKIE_SECURE"]:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if current_user.is_authenticated or request.endpoint not in (None, "static"):
            h["Cache-Control"] = "no-store"
        h["X-Request-ID"] = getattr(g, "request_id", "")
        if os.environ.get("BANK_EXPOSE_INSTANCE") == "1":  # load-balancing tests only
            import socket
            h["X-Instance"] = socket.gethostname()
        if request.endpoint != "static":
            app.logger.info("request", extra={
                "event": "http", "method": request.method, "path": request.path, "status": resp.status_code,
                "ms": round((time.perf_counter() - getattr(g, "started", time.perf_counter())) * 1000, 1),
                "user": current_user.id if current_user.is_authenticated else None})
        return resp

    @app.context_processor
    def inject_globals():
        ctx = {"csrf_token": csrf_token, "form_nonce": form_nonce, "MODULE_LABELS": models.MODULE_LABELS,
               "LOAN_TYPES": models.LOAN_TYPES, "now": datetime.now(),
               "bank_name": models.Setting.get("bank_name")}
        if current_user.is_authenticated:
            from .services import visible_alerts
            ctx["unread_alerts"] = visible_alerts(current_user).filter_by(is_read=False, is_resolved=False).count()
            ctx["session_timeout"] = models.Setting.get_int("session_timeout_minutes")
            from . import workqueue
            ctx["work"] = workqueue.counts()
        return ctx

    from .utils import TZ_OFFSET
    app.jinja_env.filters["inr"] = inr
    app.jinja_env.filters["inr_short"] = inr_short
    app.jinja_env.filters["tone"] = status_tone
    app.jinja_env.filters["ago"] = time_ago

    def cell(v):
        from decimal import Decimal
        if v is None:
            return ""
        if isinstance(v, (Decimal, float)):
            return f"{v:,.2f}"
        return v
    app.jinja_env.filters["cell"] = cell
    app.jinja_env.filters["reject_keys"] = lambda d, *keys: {k: v for k, v in d.items() if k not in keys}
    app.jinja_env.globals["RANGE_LABELS"] = RANGE_LABELS
    app.jinja_env.globals["search_term"] = private_search.term
    app.jinja_env.globals["qarg"] = private_search.args_for  # url_for(..., **qarg(term)): term as a token
    # Timestamps are stored in UTC and shown in the bank's local time (IST by default).
    app.jinja_env.filters["dt"] = (lambda v, f="%d %b %Y, %I:%M %p": (v + TZ_OFFSET).strftime(f) if v else "—")
    app.jinja_env.filters["d"] = lambda v, f="%d %b %Y": v.strftime(f) if v else "—"

    from .views import register_blueprints
    register_blueprints(app)

    @app.errorhandler(500)
    def handle_500(err):
        # Full exception goes to the server log with a reference; the employee sees only the reference.
        incident = g.get("request_id", uuid.uuid4().hex)[:12].upper()
        original = getattr(err, "original_exception", err)
        app.logger.error("unhandled error", exc_info=original, extra={"event": "error", "incident": incident,
                                                                     "path": request.path})
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 - the database itself may be the failure
            app.logger.warning("rollback after error failed", extra={"event": "rollback_failed", "incident": incident})
        return render_template("errors/error.html", code=500, title="Something went wrong",
                               message=f"The action could not be completed and nothing was saved. "
                                       f"Quote reference {incident} to your administrator."), 500

    @app.errorhandler(429)
    def handle_429(err):
        if request.path.startswith(("/healthz", "/readyz")):
            return jsonify(error="rate limited"), 429
        return render_template("errors/error.html", code=429, title="Too many requests",
                               message="Too many attempts in a short time. Wait a minute and try again."), 429

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def handle_error(err):
        titles = {400: "Bad request", 403: "Access denied", 404: "Not found", 405: "Not allowed",
                  413: "File too large"}
        messages = {403: "You do not have permission to perform this action. This attempt has been recorded.",
                    404: "The page or record you requested does not exist.",
                    405: "That action is not available here.",
                    413: "Uploaded files must be 10 MB or smaller."}
        code = getattr(err, "code", 500)
        return render_template("errors/error.html", code=code, title=titles.get(code, "Error"),
                               message=messages.get(code, getattr(err, "description", ""))), code

    from .seed import register_cli
    register_cli(app)
    from . import demo
    demo.register(app)  # `flask seed-demo`: refuses unless BANK_DEMO_DATA_ALLOWED=1
    # Schema is managed by Alembic migrations (bootstrap.prepare_database / flask db upgrade).
    return app
