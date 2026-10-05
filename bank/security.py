import re
import secrets
from functools import wraps

from flask import abort, request, session
from flask_login import current_user

from .extensions import db
from .models import Activity, AuditLog


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------

def require_perm(module, action="view"):
    """Route decorator: the logged-in employee must hold `action` on `module`."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.can(module, action):
                audit("Access denied", module, details=f"{action} on {request.path}")
                db.session.commit()
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_any_perm(*pairs):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not any(current_user.can(m, a) for m, a in pairs):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def branch_scope(query, model):
    """Least privilege: employees only see their own branch unless they are
    super admins (or hold org-wide branch management rights)."""
    if current_user.sees_all_branches:
        branch = request.args.get("branch", type=int)
        if branch:
            return query.filter(model.branch_id == branch)
        return query
    return query.filter(model.branch_id == current_user.branch_id)


def ensure_branch_access(obj):
    if obj is None:
        abort(404)
    if not current_user.sees_all_branches and obj.branch_id != current_user.branch_id:
        audit("Cross-branch access denied", details=f"{type(obj).__name__} #{obj.id}")
        db.session.commit()
        abort(403)
    return obj


def customer_from_request():
    """Resolve the customer for a create form: ?customer=<id> or ?ref=<CIF / account no>."""
    from flask import flash
    from .models import Account, Customer
    cid = request.values.get("customer", type=int)
    if cid:
        return ensure_branch_access(db.session.get(Customer, cid))
    ref = request.args.get("ref", "").strip()
    if ref:
        c = Customer.query.filter_by(customer_code=ref.upper()).first()
        if not c:
            acc = Account.query.filter_by(account_number=ref).first()
            c = acc.customer if acc else None
        if c and (current_user.sees_all_branches or c.branch_id == current_user.branch_id):
            return c
        flash("No customer found with that customer ID or account number in your branch.", "warning")
    return None


def can_view_sensitive():
    return current_user.can("customer_profile", "edit")


# --------------------------------------------------------------------------
# Password policy
# --------------------------------------------------------------------------

PASSWORD_RULES = [
    (r".{8,}", "at least 8 characters"),
    (r"[A-Z]", "an uppercase letter"),
    (r"[a-z]", "a lowercase letter"),
    (r"\d", "a digit"),
    (r"[^A-Za-z0-9]", "a special character"),
]


def password_problems(password):
    return [msg for pattern, msg in PASSWORD_RULES if not re.search(pattern, password or "")]


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def verify_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, description="Your session form token is invalid or expired. Please retry.")


# --------------------------------------------------------------------------
# Audit & activity
# --------------------------------------------------------------------------

def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def audit(action, module=None, reference=None, details=None, employee=None):
    emp = employee or (current_user if current_user and current_user.is_authenticated else None)
    db.session.add(AuditLog(
        employee_id=emp.id if emp else None,
        email=emp.email if emp else None,
        action=action, module=module, reference=reference,
        details=(details or "")[:500], ip_address=client_ip(),
    ))


def activity(customer_id, action, reference=None, details=None):
    db.session.add(Activity(
        customer_id=customer_id,
        employee_id=current_user.id if current_user.is_authenticated else None,
        action=action, reference=reference, details=details,
    ))
