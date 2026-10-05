import os
import secrets
from datetime import datetime, timedelta

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user

from .crypto import init_crypto
from .extensions import db, login_manager


def _secret_key(app):
    key = os.environ.get("BANK_SECRET_KEY")
    if key:
        return key
    path = os.path.join(app.instance_path, "secret.key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(os.path.join(app.instance_path, "uploads"), exist_ok=True)

    app.config.update(
        SECRET_KEY=_secret_key(app),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "bank.db")),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOAD_FOLDER=os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("BANK_HTTPS", "0") == "1",
        REMEMBER_COOKIE_DURATION=timedelta(seconds=0),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)
    init_crypto(app)

    from . import models  # noqa: F401
    from .security import csrf_token, verify_csrf
    from .utils import RANGE_LABELS, inr, inr_short, status_tone, time_ago

    @login_manager.user_loader
    def load_user(user_id):
        try:
            emp_id, token = user_id.split(":", 1)
        except ValueError:
            return None
        emp = db.session.get(models.Employee, int(emp_id))
        if emp and (emp.session_token or "") == token:
            return emp
        return None

    @app.before_request
    def security_gate():
        verify_csrf()
        if not current_user.is_authenticated or request.endpoint in (None, "static"):
            return
        # Inactivity timeout
        timeout = models.Setting.get_int("session_timeout_minutes")
        last = session.get("last_seen")
        now = datetime.utcnow().timestamp()
        if last and now - last > timeout * 60:
            from .security import audit
            audit("Session timeout", "auth")
            db.session.commit()
            logout_user()
            session.clear()
            flash("Your session expired due to inactivity. Please sign in again.", "warning")
            return redirect(url_for("auth.login", next=request.path))
        session["last_seen"] = now
        session.permanent = True
        # Forced password change / expiry
        allowed = {"auth.logout", "settings.change_password", "auth.keepalive"}
        if request.endpoint not in allowed:
            expiry_days = models.Setting.get_int("password_expiry_days")
            expired = (current_user.password_changed_at and expiry_days and
                       datetime.utcnow() - current_user.password_changed_at
                       > timedelta(days=expiry_days))
            if current_user.must_change_password or expired:
                flash("Please set a new password to continue.", "warning")
                return redirect(url_for("settings.change_password"))
            if (models.Setting.get("enforce_mfa") == "1" and not current_user.mfa_enabled
                    and request.endpoint not in {"settings.mfa_setup", "settings.mfa_enable"}):
                flash("Your bank requires multi-factor authentication. Please set it up.",
                      "warning")
                return redirect(url_for("settings.mfa_setup"))

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if current_user.is_authenticated:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject_globals():
        ctx = {"csrf_token": csrf_token, "MODULE_LABELS": models.MODULE_LABELS,
               "LOAN_TYPES": models.LOAN_TYPES, "now": datetime.now(),
               "bank_name": models.Setting.get("bank_name")}
        if current_user.is_authenticated:
            from .services import visible_alerts
            ctx["unread_alerts"] = visible_alerts(current_user).filter_by(
                is_read=False, is_resolved=False).count()
            ctx["session_timeout"] = models.Setting.get_int("session_timeout_minutes")
        return ctx

    app.jinja_env.filters["inr"] = inr
    app.jinja_env.filters["inr_short"] = inr_short
    app.jinja_env.filters["tone"] = status_tone
    app.jinja_env.filters["ago"] = time_ago
    app.jinja_env.globals["RANGE_LABELS"] = RANGE_LABELS
    # Timestamps are stored in UTC and shown in the bank's local time (IST by default).
    tz_offset = timedelta(minutes=int(os.environ.get("BANK_TZ_OFFSET_MINUTES", "330")))
    app.jinja_env.filters["dt"] = (lambda v, f="%d %b %Y, %I:%M %p":
                                   (v + tz_offset).strftime(f) if v else "—")
    app.jinja_env.filters["d"] = lambda v, f="%d %b %Y": v.strftime(f) if v else "—"

    from .views import register_blueprints
    register_blueprints(app)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def handle_error(err):
        titles = {400: "Bad request", 403: "Access denied", 404: "Not found",
                  413: "File too large"}
        messages = {403: "You do not have permission to perform this action. "
                         "This attempt has been recorded.",
                    404: "The page or record you requested does not exist.",
                    413: "Uploaded files must be 10 MB or smaller."}
        code = getattr(err, "code", 500)
        return render_template("errors/error.html", code=code, title=titles.get(code, "Error"),
                               message=messages.get(code, getattr(err, "description", ""))), code

    from .seed import register_cli
    register_cli(app)

    with app.app_context():
        db.create_all()

    return app
