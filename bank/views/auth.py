import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pyotp
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from ..crypto import decrypt
from ..extensions import db, limiter
from ..models import AlertRule, Employee, InviteToken, LoginHistory, Setting
from ..security import audit, client_ip, password_problems
from ..services import failed_login_alert, raise_alert

bp = Blueprint("auth", __name__)

# Same message for every failure, so the page never reveals whether an email belongs to an
# employee, or whether that account is locked or disabled.
GENERIC_FAILURE = ("Sign-in failed. Check your email and password. After repeated failures an account "
                   "is locked for a short time; contact your administrator if this continues.")
_DUMMY_HASH = generate_password_hash("timing-equaliser", method="scrypt")
LOGIN_LIMIT = "10 per minute;60 per hour"


def _safe_next(target):
    if target and urlparse(target).netloc == "" and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard.index")


def _record(emp, email, success, reason):
    db.session.add(LoginHistory(employee_id=emp.id if emp else None, email=email[:160],
                                ip_address=client_ip(), user_agent=(request.user_agent.string or "")[:255],
                                success=success, reason=reason))


def _register_failure(emp, email, reason):
    _record(emp, email, False, reason)
    if emp:
        emp.failed_attempts = (emp.failed_attempts or 0) + 1
        if emp.failed_attempts >= Setting.get_int("max_failed_logins"):
            emp.locked_until = datetime.utcnow() + timedelta(minutes=Setting.get_int("lockout_minutes"))
            audit("Account locked", "auth", emp.email, f"Locked after {emp.failed_attempts} failed attempts",
                  employee=emp, result="denied")
        failed_login_alert(email, emp.failed_attempts, emp)
    audit("Failed login", "auth", email[:80], reason, employee=emp, result="failed")
    db.session.commit()


def _new_network_check(emp):
    """Alert when an employee signs in from an IP not used successfully in the last 30 days."""
    rule = AlertRule.query.filter_by(rule_type="new_login_ip", enabled=True).first()
    if not rule:
        return
    ip = client_ip()
    since = datetime.utcnow() - timedelta(days=30)
    seen_before = LoginHistory.query.filter(LoginHistory.employee_id == emp.id, LoginHistory.success.is_(True),
                                            LoginHistory.timestamp >= since).first()
    if seen_before and not LoginHistory.query.filter(
            LoginHistory.employee_id == emp.id, LoginHistory.success.is_(True),
            LoginHistory.timestamp >= since, LoginHistory.ip_address == ip).first():
        raise_alert("failed_login", "Sign-in from a new network",
                    f"{emp.name} signed in from {ip}, not used in the last 30 days.",
                    module=rule.route_module, severity=rule.severity,
                    reference=f"NEWIP-{emp.id}-{ip}", link="/users/login-history", branch_id=emp.branch_id)


def _complete_login(emp):
    _new_network_check(emp)
    emp.failed_attempts = 0
    emp.locked_until = None
    emp.last_login = datetime.utcnow()
    emp.last_login_ip = client_ip()
    if not emp.session_token:
        emp.session_token = secrets.token_hex(16)
    session.clear()  # new session id on sign-in (no session fixation)
    login_user(emp, remember=False)
    now = datetime.utcnow().timestamp()
    session["last_seen"] = session["login_at"] = now
    _record(emp, emp.email, True, "Success")
    audit("Login", "auth", emp.email, employee=emp)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(LOGIN_LIMIT, methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:160]
        password = request.form.get("password", "")[:200]
        emp = Employee.query.filter(db.func.lower(Employee.email) == email).first()
        if not emp:
            check_password_hash(_DUMMY_HASH, password)  # same work as a real check: no timing signal
            _register_failure(None, email, "Unknown email")
            flash(GENERIC_FAILURE, "danger")
            return render_template("auth/login.html", email=email)
        if emp.locked_until and emp.locked_until > datetime.utcnow():
            emp.check_password(password)
            _record(emp, email, False, "Account locked")
            db.session.commit()
            flash(GENERIC_FAILURE, "danger")
            return render_template("auth/login.html", email=email)
        if not emp.check_password(password):
            _register_failure(emp, email, "Bad credentials")
            flash(GENERIC_FAILURE, "danger")
            return render_template("auth/login.html", email=email)
        if not emp.is_active:
            _register_failure(emp, email, "Account disabled")
            flash(GENERIC_FAILURE, "danger")
            return render_template("auth/login.html", email=email)
        if emp.mfa_enabled:
            session.clear()
            session["mfa_pending"] = emp.id
            session["mfa_started"] = datetime.utcnow().timestamp()
            session["mfa_next"] = request.args.get("next")
            return redirect(url_for("auth.mfa"))
        _complete_login(emp)
        return redirect(_safe_next(request.args.get("next")))
    return render_template("auth/login.html")


def verify_totp(emp, code):
    """Accept the current code (±1 step for clock drift) but never the same or an older step
    twice, so a code seen over someone's shoulder cannot be replayed."""
    if not emp.mfa_secret or not code.isdigit() or len(code) != 6:
        return False
    totp = pyotp.TOTP(decrypt(emp.mfa_secret))
    now = datetime.now(timezone.utc)  # must be timezone-aware: pyotp treats naive times as server-local
    for offset in (-1, 0, 1):
        moment = now + timedelta(seconds=30 * offset)
        if secrets.compare_digest(totp.at(moment), code):
            step = totp.timecode(moment)
            if step <= (emp.mfa_last_step or 0):
                return False
            emp.mfa_last_step = step
            return True
    return False


@bp.route("/login/mfa", methods=["GET", "POST"])
@limiter.limit(LOGIN_LIMIT, methods=["POST"])
def mfa():
    emp_id = session.get("mfa_pending")
    started = session.get("mfa_started", 0)
    if not emp_id or datetime.utcnow().timestamp() - started > 300:
        session.clear()
        flash("Verification timed out. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))
    emp = db.session.get(Employee, emp_id)
    if not emp or not emp.is_active:
        session.clear()
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        if verify_totp(emp, code):
            nxt = session.get("mfa_next")
            _complete_login(emp)
            return redirect(_safe_next(nxt))
        _register_failure(emp, emp.email, "Invalid or reused MFA code")
        if emp.locked_until and emp.locked_until > datetime.utcnow():
            session.clear()
            flash(GENERIC_FAILURE, "danger")
            return redirect(url_for("auth.login"))
        flash("Invalid verification code.", "danger")
    return render_template("auth/mfa.html", email=emp.email)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("Logout", "auth")
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out securely.", "success")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------
# Invite / reset links: an employee sets their own password; nobody else ever sees it.
# --------------------------------------------------------------------------

def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_invite(emp, purpose, created_by, hours=48):
    """Create a one-time link. Older unused links for the same employee stop working."""
    InviteToken.query.filter(InviteToken.employee_id == emp.id, InviteToken.used_at == None).update(  # noqa: E711
        {"used_at": datetime.utcnow()}, synchronize_session=False)
    raw = secrets.token_urlsafe(32)
    db.session.add(InviteToken(token_hash=token_hash(raw), employee_id=emp.id, purpose=purpose,
                               expires_at=datetime.utcnow() + timedelta(hours=hours), created_by=created_by))
    return raw


@bp.route("/invite/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def accept_invite(token):
    if len(token) > 100:
        abort(404)
    inv = InviteToken.query.filter_by(token_hash=token_hash(token)).first()
    if not inv or inv.used_at or inv.expires_at < datetime.utcnow() or not inv.employee.is_active:
        audit("Invalid invite link used", "auth", details="expired, used or unknown link", result="denied")
        db.session.commit()
        return render_template("auth/invite.html", invalid=True), 404
    emp = inv.employee
    if request.method == "POST":
        pw, confirm = request.form.get("password", ""), request.form.get("confirm", "")
        problems = password_problems(pw, emp.email)
        if pw != confirm:
            flash("The two passwords do not match.", "danger")
        elif problems:
            flash("Password needs " + ", ".join(problems) + ".", "danger")
        else:
            emp.set_password(pw)
            emp.must_change_password = False
            emp.session_token = secrets.token_hex(16)
            emp.failed_attempts = 0
            emp.locked_until = None
            inv.used_at = datetime.utcnow()
            audit("Password set from invite link", "auth", emp.email, inv.purpose, employee=emp)
            db.session.commit()
            flash("Your password is set. Sign in to continue.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/invite.html", invalid=False, emp=emp, purpose=inv.purpose)
