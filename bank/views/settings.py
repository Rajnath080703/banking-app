from datetime import datetime, timezone
import base64
import io
import secrets

import pyotp
import qrcode
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user

from ..crypto import encrypt
from ..extensions import db
from ..models import Branch, LoginHistory, Setting
from ..security import audit, once_per_form, password_problems, require_perm
from ..money import ZERO, parse_money

bp = Blueprint("settings", __name__, url_prefix="/settings")

# key, label, kind, (min, max) for numbers — every value is validated server-side.
EDITABLE = [
    ("bank_name", "Bank name", "text", None),
    ("session_timeout_minutes", "Session timeout (minutes of inactivity)", "number", (1, 120)),
    ("absolute_session_hours", "Maximum session length (hours)", "number", (1, 24)),
    ("max_failed_logins", "Failed logins before lockout", "number", (3, 10)),
    ("lockout_minutes", "Lockout duration (minutes)", "number", (5, 1440)),
    ("password_expiry_days", "Password expiry (days, 0 = never)", "number", (0, 365)),
    ("kyc_expiry_warning_days", "KYC expiry warning window (days)", "number", (1, 180)),
    ("emi_due_warning_days", "EMI due reminder window (days)", "number", (1, 30)),
    ("large_txn_approval_limit", "High-value transaction approval limit (₹)", "number", (10000, 100000000)),
    ("penal_rate_pa", "Penal charge on overdue EMI (% a year)", "number", (0, 36)),
    ("fd_premature_penalty_pct", "FD premature-closure penalty (% off the rate)", "number", (0, 3)),
    ("max_export_rows", "Largest report export (rows)", "number", (100, 500000)),
    ("tds_rate_pct", "TDS on FD interest, with PAN (%)", "number", (0, 30)),
    ("tds_rate_no_pan_pct", "TDS on FD interest, without PAN (%)", "number", (0, 40)),
    ("tds_threshold", "Yearly FD interest before TDS (₹)", "number", (0, 10000000)),
    ("tds_threshold_senior", "Yearly FD interest before TDS, senior citizens (₹)", "number", (0, 10000000)),
]


def _visible_branches():
    return Branch.query.order_by(Branch.id).all() if current_user.sees_all_branches else [current_user.branch]


@bp.route("/", methods=["GET", "POST"])
@login_required
@require_perm("settings", "view")
@once_per_form
def index():
    if request.method == "POST":
        if not current_user.can("settings", "edit") and not current_user.can("branch_management", "edit"):
            abort(403)
        problems, changed = [], []
        if current_user.can("settings", "edit"):
            for key, label, kind, limits in EDITABLE:
                raw = request.form.get(key, "").strip()
                if kind == "text":
                    if raw and raw != Setting.get(key):
                        Setting.set(key, raw[:80])
                        changed.append(key)
                    continue
                try:
                    val = int(raw)
                except ValueError:
                    problems.append(f"{label}: enter a whole number.")
                    continue
                lo, hi = limits
                if not lo <= val <= hi:
                    problems.append(f"{label}: must be between {lo:,} and {hi:,}.")
                    continue
                if str(val) != str(Setting.get(key)):
                    Setting.set(key, str(val))
                    changed.append(key)
            mfa = "1" if request.form.get("enforce_mfa") else "0"
            if mfa != Setting.get("enforce_mfa"):
                Setting.set("enforce_mfa", mfa)
                changed.append("enforce_mfa")
        if current_user.can("branch_management", "edit"):
            # Only the branches this employee may manage: their own, or all for head office.
            for b in _visible_branches():
                name = request.form.get(f"b{b.id}_name", "").strip()
                if name and len(name) >= 3:
                    b.name = name[:120]
                b.city = (request.form.get(f"b{b.id}_city", b.city or "") or "").strip()[:80]
                for field, form_key in (("target_deposits", "dep"), ("target_loans", "loan")):
                    raw = request.form.get(f"b{b.id}_{form_key}")
                    if raw is not None and raw.strip() != "":
                        val = parse_money(raw, ZERO)
                        if val is None:
                            problems.append(f"{b.name}: targets must be amounts of ₹0 or more.")
                        else:
                            setattr(b, field, val)
                acc = request.form.get(f"b{b.id}_acc", type=int)
                if acc is not None and acc >= 0:
                    b.target_accounts = acc
        for p in problems:
            flash(p, "danger")
        audit("Settings updated", "settings", details=", ".join(changed)[:480] or "branch targets")
        db.session.commit()
        if not problems:
            flash("Settings saved.", "success")
        return redirect(url_for("settings.index"))
    values = {k: Setting.get(k) for k, _, _, _ in EDITABLE}
    return render_template("settings/index.html", fields=[(k, label, kind) for k, label, kind, _ in EDITABLE],
                           values=values, enforce_mfa=Setting.get("enforce_mfa") == "1", branches=_visible_branches())


@bp.route("/branches/new", methods=["POST"])
@login_required
@require_perm("branch_management", "create")
@once_per_form
def branch_new():
    if current_user.role.level < 90:  # opening a branch is a head-office decision
        abort(403)
    import re
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    if not re.fullmatch(r"[A-Z0-9]{3,12}", code) or len(name) < 3:
        flash("Branch code must be 3–12 letters/digits and the name at least 3 characters.", "danger")
    elif Branch.query.filter_by(code=code).first():
        flash(f"A branch with code {code} already exists.", "danger")
    else:
        db.session.add(Branch(code=code, name=name, city=request.form.get("city", "").strip(),
                              target_deposits=parse_money(request.form.get("dep") or "0", ZERO) or ZERO,
                              target_loans=parse_money(request.form.get("loan") or "0", ZERO) or ZERO,
                              target_accounts=request.form.get("acc", type=int) or 0))
        audit("Branch created", "branch_management", code, name)
        db.session.commit()
        flash(f"Branch {name} ({code}) added.", "success")
    return redirect(url_for("settings.index") + "#branches")


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
        elif password_problems(new, current_user.email):
            flash("Password needs " + ", ".join(password_problems(new, current_user.email)) + ".", "danger")
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
        current_user.mfa_secret = encrypt(secret)  # stored encrypted, like other secrets
        current_user.mfa_enabled = True
        # the enrolment code counts as used, so it cannot be replayed at the next sign-in
        current_user.mfa_last_step = pyotp.TOTP(secret).timecode(datetime.now(timezone.utc))
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
