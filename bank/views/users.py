import re
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mailer import send_mail
from ..models import ACTIONS, MODULES, Branch, Employee, LoginHistory, Permission, Role
from ..private_search import term as search_term
from ..security import audit, once_per_form, require_perm
from ..services import apply_role_grants, create_approval, pending_request
from .auth import issue_invite

bp = Blueprint("users", __name__, url_prefix="/users")


def _deliver_link(emp, raw, purpose):
    """Email the one-time link when SMTP is configured; otherwise show it once on screen.
    The link is rendered directly (never put in a flash message, which lives in the cookie)."""
    link = url_for("auth.accept_invite", token=raw, _external=True)
    subject = "Your Local Bank Ops account" if purpose == "invite" else "Reset your Local Bank Ops password"
    sent = send_mail(emp.email, subject,
                     f"Hello {emp.name},\n\nUse this one-time link within 48 hours to set your password:\n{link}\n\n"
                     f"If you did not expect this message, tell your administrator.")
    audit("Invite link issued" if purpose == "invite" else "Password reset link issued", "user_management",
          emp.email, "emailed" if sent else "shown to administrator")
    db.session.commit()
    return render_template("users/invite_link.html", emp=emp, link=None if sent else link, purpose=purpose)


def _assignable_roles():
    """Least privilege: you can only assign roles below your own level."""
    q = Role.query.order_by(Role.level.desc())
    if not current_user.is_super_admin:
        q = q.filter(Role.level < current_user.role.level)
    return q.all()


def role_grants(role):
    """{module: [actions]} — what a role allows (super admin: everything)."""
    if role.level >= 100:
        return {m: list(ACTIONS) for m, _ in MODULES}
    return {p.module: [a for a in ACTIONS if p.allows(a)] for p in role.permissions}


def _posted_grants(form):
    """Permissions ticked on the form, capped to what the editor holds themselves."""
    out = {}
    for m, _ in MODULES:
        acts = [a for a in ACTIONS if form.get(f"{m}:{a}") and current_user.can(m, a)]
        if acts:
            out[m] = acts
    return out


def _same_grants(a, b):
    norm = lambda g: {m: sorted(v) for m, v in g.items() if v}  # noqa: E731
    return norm(a) == norm(b)


def _can_manage(emp):
    if emp.id == current_user.id:
        return False
    return current_user.is_super_admin or emp.role.level < current_user.role.level


def _scoped_employees():
    q = Employee.query
    if not current_user.sees_all_branches:
        q = q.filter(Employee.branch_id == current_user.branch_id)
    return q


@bp.route("/")
@login_required
@require_perm("user_management", "view")
def index():
    q = _scoped_employees()
    term = search_term()
    role_id = request.args.get("role", type=int)
    if term:
        q = q.filter(db.or_(Employee.name.ilike(f"%{term}%"), Employee.email.ilike(f"%{term}%")))
    if role_id:
        q = q.filter(Employee.role_id == role_id)
    employees = q.join(Role).order_by(Role.level.desc(), Employee.name).all()
    return render_template("users/index.html", employees=employees, roles=Role.query.order_by(Role.level.desc()).all(),
                           term=term, role_id=role_id, can_manage=_can_manage)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("user_management", "create")
@once_per_form
def new():
    roles = _assignable_roles()
    branches = Branch.query.all() if current_user.sees_all_branches else [current_user.branch]
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        name = f.get("name", "").strip()
        role = db.session.get(Role, f.get("role_id", type=int) or 0)
        branch_id = f.get("branch_id", type=int)
        errors = []
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("Enter a valid email address.")
        elif Employee.query.filter(db.func.lower(Employee.email) == email).first():
            errors.append("An employee with this email already exists.")
        if len(name) < 2:
            errors.append("Name is required.")
        if role not in roles:
            errors.append("You are not allowed to assign that role.")
        if branch_id not in [b.id for b in branches]:
            errors.append("Invalid branch.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("users/new.html", roles=roles, branches=branches, f=f,
                                   modules=MODULES, actions=ACTIONS, grants=_posted_grants(f),
                                   role_map={r.id: role_grants(r) for r in roles})
        # If the permission grid was customised, give the employee a dedicated custom role
        # derived from the selected one (never above the editor's own rights or level).
        posted = _posted_grants(f)
        if f.get("perms_submitted") and role.level < 100 and not _same_grants(posted, role_grants(role)):
            base_name = f"Custom – {name}"
            role_name, n = base_name, 2
            while Role.query.filter_by(name=role_name).first():
                role_name, n = f"{base_name} ({n})", n + 1
            custom = Role(name=role_name, level=role.level, is_system=False,
                          description=f"Based on {role.name}, customised for {email}")
            for m, _ in MODULES:
                p = Permission(module=m)
                for a in posted.get(m, []):
                    setattr(p, f"can_{a}", True)
                custom.permissions.append(p)
            db.session.add(custom)
            db.session.flush()
            audit("Role created", "user_management", role_name, f"custom permissions for {email}")
            role = custom
        count = Employee.query.count() + 1
        emp = Employee(employee_code=f"EMP{count:04d}", name=name[:120], email=email, phone=(f.get("phone") or "")[:20],
                       role=role, branch_id=branch_id, session_token=secrets.token_hex(16))
        while Employee.query.filter_by(employee_code=emp.employee_code).first():
            count += 1
            emp.employee_code = f"EMP{count:04d}"
        emp.set_password(secrets.token_urlsafe(48))  # unusable until the employee sets their own
        db.session.add(emp)
        db.session.flush()
        audit("Employee created", "user_management", email, f"role={role.name}")
        raw = issue_invite(emp, "invite", current_user.id)
        return _deliver_link(emp, raw, "invite")
    default_role = next((r for r in roles if r.name == "Clerk"), roles[-1] if roles else None)
    return render_template("users/new.html", roles=roles, branches=branches, f={"role_id": default_role.id if default_role else None},
                           modules=MODULES, actions=ACTIONS, grants=role_grants(default_role) if default_role else {},
                           role_map={r.id: role_grants(r) for r in roles})


@bp.route("/<int:eid>/edit", methods=["GET", "POST"])
@login_required
@require_perm("user_management", "edit")
@once_per_form
def edit(eid):
    emp = db.session.get(Employee, eid) or abort(404)
    if not _can_manage(emp) or (not current_user.sees_all_branches and emp.branch_id != current_user.branch_id):
        abort(403)
    roles = _assignable_roles()
    branches = Branch.query.all() if current_user.sees_all_branches else [current_user.branch]
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        if action == "save":
            role = db.session.get(Role, f.get("role_id", type=int) or 0)
            if role not in roles:
                abort(403)
            changes = []
            if role.id != emp.role_id:
                changes.append(f"role {emp.role.name} → {role.name}")
                emp.role = role
            branch_id = f.get("branch_id", type=int)
            if branch_id in [b.id for b in branches] and branch_id != emp.branch_id:
                changes.append("branch changed")
                emp.branch_id = branch_id
            active = bool(f.get("active"))
            if active != emp.is_active_flag:
                changes.append("activated" if active else "deactivated")
                emp.is_active_flag = active
                if not active:
                    emp.session_token = secrets.token_hex(16)
            emp.name = (f.get("name", emp.name).strip() or emp.name)[:120]
            emp.phone = f.get("phone", "").strip()[:20]
            audit("Employee updated", "user_management", emp.email, "; ".join(changes) or "details")
            flash("Employee updated.", "success")
        elif action == "reset":
            emp.set_password(secrets.token_urlsafe(48))  # old password stops working immediately
            emp.session_token = secrets.token_hex(16)    # and every open session ends
            audit("Password reset", "user_management", emp.email)
            raw = issue_invite(emp, "reset", current_user.id)
            return _deliver_link(emp, raw, "reset")
        elif action == "unlock":
            emp.failed_attempts = 0
            emp.locked_until = None
            audit("Employee updated", "user_management", emp.email, "unlocked")
            flash("Account unlocked.", "success")
        elif action == "revoke":
            emp.session_token = secrets.token_hex(16)
            audit("Sessions revoked", "user_management", emp.email)
            flash("All active sessions for this employee were signed out.", "success")
        elif action == "reset_mfa":
            emp.mfa_enabled = False
            emp.mfa_secret = None
            audit("MFA disabled", "user_management", emp.email, "reset by admin")
            flash("MFA reset. The employee must enrol again.", "success")
        db.session.commit()
        return redirect(url_for("users.edit", eid=emp.id))
    history = LoginHistory.query.filter_by(employee_id=emp.id).order_by(LoginHistory.timestamp.desc()).limit(15).all()
    return render_template("users/form.html", roles=roles, branches=branches, f={}, emp=emp, history=history,
                           modules=MODULES, actions=ACTIONS, grants=role_grants(emp.role))


@bp.route("/roles")
@login_required
@require_perm("user_management", "view")
def roles():
    roles = Role.query.order_by(Role.level.desc()).all()
    counts = dict(db.session.query(Employee.role_id, db.func.count()).group_by(Employee.role_id).all())
    return render_template("users/roles.html", roles=roles, counts=counts, modules=MODULES, actions=ACTIONS)


@bp.route("/roles/new", methods=["POST"])
@login_required
@require_perm("user_management", "create")
def role_new():
    name = request.form.get("name", "").strip()
    if len(name) < 3 or Role.query.filter(db.func.lower(Role.name) == name.lower()).first():
        flash("Role name is required and must be unique.", "danger")
        return redirect(url_for("users.roles"))
    level = min(request.form.get("level", type=int) or 30, current_user.role.level - 1 if not current_user.is_super_admin else 99)
    role = Role(name=name, description=request.form.get("description"), level=max(level, 1))
    for module, _ in MODULES:
        role.permissions.append(Permission(module=module))
    db.session.add(role)
    db.session.flush()
    audit("Role created", "user_management", name)
    db.session.commit()
    flash(f"Role {name} created. Configure its permissions below.", "success")
    return redirect(url_for("users.role_edit", rid=role.id))


@bp.route("/roles/<int:rid>", methods=["GET", "POST"])
@login_required
@require_perm("user_management", "view")
def role_edit(rid):
    role = db.session.get(Role, rid) or abort(404)
    editable = (current_user.can("user_management", "edit") and role.level < 100
                and (current_user.is_super_admin or role.level < current_user.role.level))
    if request.method == "POST":
        if not editable:
            abort(403)
        current = role_grants(role)
        wanted = {}
        for module, _ in MODULES:
            acts = []
            for a in ACTIONS:
                on = bool(request.form.get(f"{module}:{a}"))
                # Never grant a right the editor does not hold; rights the editor lacks stay as they are.
                if not current_user.can(module, a):
                    on = a in current.get(module, [])
                if on:
                    acts.append(a)
            if acts:
                wanted[module] = acts
        changes = [f"{m}.{a}={'Y' if a in wanted.get(m, []) else 'N'}" for m, _ in MODULES for a in ACTIONS
                   if (a in wanted.get(m, [])) != (a in current.get(m, []))]
        if not role.is_system and current_user.is_super_admin:
            role.description = (request.form.get("description") or role.description or "")[:255]
        if not changes:
            flash("No permission changes to save.", "info")
        elif current_user.is_super_admin:
            apply_role_grants(role, wanted)
            audit("Permissions updated", "user_management", role.name, ", ".join(changes)[:480])
            flash(f"Permissions for {role.name} saved ({len(changes)} change{'s' if len(changes) != 1 else ''}).", "success")
        elif pending_request("role_permissions", role.id):
            flash("A permission change for this role is already waiting for approval.", "warning")
        else:
            req = create_approval("role_permissions", "role", role.id, None, None,
                                  f"Permission change for role {role.name}: {len(changes)} change(s)",
                                  {"grants": wanted, "changes": changes})
            flash(f"Permission change sent for approval ({req.request_number}). It takes effect once a "
                  f"second administrator approves it.", "success")
        db.session.commit()
        return redirect(url_for("users.role_edit", rid=role.id))
    return render_template("users/role_edit.html", role=role, modules=MODULES, actions=ACTIONS, editable=editable,
                           grants=role_grants(role))


@bp.route("/login-history")
@login_required
@require_perm("user_management", "view")
def login_history():
    q = LoginHistory.query
    if not current_user.sees_all_branches:
        q = q.join(Employee).filter(Employee.branch_id == current_user.branch_id)
    if request.args.get("failed"):
        q = q.filter(LoginHistory.success.is_(False))
    page = request.args.get("page", 1, type=int)
    rows = q.order_by(LoginHistory.timestamp.desc()).paginate(page=page, per_page=40, error_out=False)
    return render_template("users/login_history.html", rows=rows)
