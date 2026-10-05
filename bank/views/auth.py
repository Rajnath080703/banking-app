import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pyotp
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import Employee, LoginHistory, Setting
from ..security import audit, client_ip
from ..services import failed_login_alert

bp = Blueprint("auth", __name__)


def _safe_next(target):
    if target and urlparse(target).netloc == "" and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard.index")


def _record(emp, email, success, reason):
    db.session.add(LoginHistory(employee_id=emp.id if emp else None, email=email,
                                ip_address=client_ip(),
                                user_agent=(request.user_agent.string or "")[:255],
                                success=success, reason=reason))


def _register_failure(emp, email, reason):
    _record(emp, email, False, reason)
    if emp:
        emp.failed_attempts = (emp.failed_attempts or 0) + 1
        max_attempts = Setting.get_int("max_failed_logins")
        if emp.failed_attempts >= max_attempts:
            emp.locked_until = datetime.utcnow() + timedelta(minutes=Setting.get_int("lockout_minutes"))
            audit("Account locked", "auth", emp.email,
                  f"Locked after {emp.failed_attempts} failed attempts", employee=emp)
        failed_login_alert(email, emp.failed_attempts, emp)
    audit("Failed login", "auth", email, reason, employee=emp)
    db.session.commit()


def _complete_login(emp):
    emp.failed_attempts = 0
    emp.locked_until = None
    emp.last_login = datetime.utcnow()
    if not emp.session_token:
        emp.session_token = secrets.token_hex(16)
    session.clear()
    login_user(emp, remember=False)
    session["last_seen"] = datetime.utcnow().timestamp()
    _record(emp, emp.email, True, "Success")
    audit("Login", "auth", emp.email, employee=emp)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        emp = Employee.query.filter(db.func.lower(Employee.email) == email).first()
        generic = "Invalid email or password."
        if emp and emp.locked_until and emp.locked_until > datetime.utcnow():
            mins = int((emp.locked_until - datetime.utcnow()).total_seconds() // 60) + 1
            _record(emp, email, False, "Account locked")
            db.session.commit()
            flash(f"Account temporarily locked after repeated failures. Try again in {mins} min.", "danger")
            return render_template("auth/login.html", email=email)
        if not emp or not emp.check_password(password):
            _register_failure(emp, email, "Bad credentials")
            flash(generic, "danger")
            return render_template("auth/login.html", email=email)
        if not emp.is_active:
            _register_failure(emp, email, "Account disabled")
            flash("This account is disabled. Contact your administrator.", "danger")
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


@bp.route("/login/mfa", methods=["GET", "POST"])
def mfa():
    emp_id = session.get("mfa_pending")
    started = session.get("mfa_started", 0)
    if not emp_id or datetime.utcnow().timestamp() - started > 300:
        session.clear()
        flash("Verification timed out. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))
    emp = db.session.get(Employee, emp_id)
    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        if emp and pyotp.TOTP(emp.mfa_secret).verify(code, valid_window=1):
            nxt = session.get("mfa_next")
            _complete_login(emp)
            return redirect(_safe_next(nxt))
        _register_failure(emp, emp.email if emp else "", "Invalid MFA code")
        if emp.locked_until and emp.locked_until > datetime.utcnow():
            session.clear()
            flash("Too many failed attempts. Account locked.", "danger")
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


@bp.route("/keepalive", methods=["POST"])
@login_required
def keepalive():
    return jsonify(ok=True)
