import base64
import io
import secrets

import pyotp
import qrcode
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user

from ..extensions import db
from ..models import Branch, LoginHistory, Setting
from ..security import audit, password_problems, require_perm
from ..utils import parse_float

bp = Blueprint("settings", __name__, url_prefix="/settings")

EDITABLE = [
    ("bank_name", "Bank name", "text"),
    ("session_timeout_minutes", "Session timeout (minutes of inactivity)", "number"),
    ("max_failed_logins", "Failed logins before lockout", "number"),
    ("lockout_minutes", "Lockout duration (minutes)", "number"),
    ("password_expiry_days", "Password expiry (days, 0 = never)", "number"),
    ("kyc_expiry_warning_days", "KYC expiry warning window (days)", "number"),
    ("emi_due_warning_days", "EMI due reminder window (days)", "number"),
]


@bp.route("/", methods=["GET", "POST"])
@login_required
@require_perm("settings", "view")
def index():
    if request.method == "POST":
        if not current_user.can("settings", "edit"):
            abort(403)
        for key, _, kind in EDITABLE:
            val = request.form.get(key, "").strip()
            if kind == "number":
                try:
                    val = str(max(0, int(val)))
                except ValueError:
                    continue
                if key == "session_timeout_minutes":
                    val = str(min(max(int(val), 1), 120))
                if key == "max_failed_logins":
                    val = str(max(int(val), 3))
            if val:
                Setting.set(key, val)
        Setting.set("enforce_mfa", "1" if request.form.get("enforce_mfa") else "0")
        if current_user.can("branch_management", "edit"):
            for b in Branch.query.all():
                b.target_deposits = parse_float(request.form.get(f"b{b.id}_dep"), b.target_deposits)
                b.target_loans = parse_float(request.form.get(f"b{b.id}_loan"), b.target_loans)
                b.target_accounts = request.form.get(f"b{b.id}_acc", type=int) or b.target_accounts
        audit("Settings updated", "settings")
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings.index"))
    values = {k: Setting.get(k) for k, _, _ in EDITABLE}
    return render_template("settings/index.html", fields=EDITABLE, values=values,
                           enforce_mfa=Setting.get("enforce_mfa") == "1", branches=Branch.query.all())


@bp.route("/profile")
@login_required
def profile():
    history = LoginHistory.query.filter_by(employee_id=current_user.id).order_by(LoginHistory.timestamp.desc()).limit(10).all()
    return render_template("settings/profile.html", history=history)


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if not current_user.check_password(current):
            flash("Current password is incorrect.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        elif current_user.check_password(new):
            flash("New password must differ from the current one.", "danger")
        elif password_problems(new):
            flash("Password needs " + ", ".join(password_problems(new)) + ".", "danger")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            current_user.session_token = secrets.token_hex(16)  # sign out other sessions
            audit("Password changed", "auth")
            db.session.commit()
            login_user(current_user._get_current_object())
            flash("Password changed. Other sessions have been signed out.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("settings/password.html")


@bp.route("/mfa", methods=["GET"])
@login_required
def mfa_setup():
    if current_user.mfa_enabled:
        return render_template("settings/mfa.html", enabled=True)
    secret = session.get("mfa_setup_secret") or pyotp.random_base32()
    session["mfa_setup_secret"] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.email,
                                              issuer_name=Setting.get("bank_name") + " Ops")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return render_template("settings/mfa.html", enabled=False, qr=qr, secret=secret)


@bp.route("/mfa/enable", methods=["POST"])
@login_required
def mfa_enable():
    secret = session.get("mfa_setup_secret")
    code = request.form.get("code", "").replace(" ", "")
    if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
        current_user.mfa_secret = secret
        current_user.mfa_enabled = True
        session.pop("mfa_setup_secret", None)
        audit("MFA enabled", "auth")
        db.session.commit()
        flash("Multi-factor authentication is now active on your account.", "success")
        return redirect(url_for("settings.profile"))
    flash("That code did not match. Check your authenticator app's time and try again.", "danger")
    return redirect(url_for("settings.mfa_setup"))


@bp.route("/mfa/disable", methods=["POST"])
@login_required
def mfa_disable():
    if Setting.get("enforce_mfa") == "1":
        flash("MFA is mandatory for all employees and cannot be disabled.", "danger")
        return redirect(url_for("settings.mfa_setup"))
    if not current_user.check_password(request.form.get("password", "")):
        flash("Password is incorrect.", "danger")
        return redirect(url_for("settings.mfa_setup"))
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    audit("MFA disabled", "auth")
    db.session.commit()
    flash("MFA disabled.", "warning")
    return redirect(url_for("settings.profile"))
