import re
import secrets
from functools import wraps

from flask import abort, flash, has_request_context, redirect, request, session
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
                audit("Access denied", module, details=f"{action} on {request.method} {request.path}", result="denied")
                db.session.commit()
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
        audit("Cross-branch access denied", details=f"{type(obj).__name__} #{obj.id}", result="denied")
        db.session.commit()
        abort(403)
    return obj


def customer_from_request():
    """Resolve the customer for a create form: ?customer=<id> or ?ref=<CIF / account no>.
    With ?find=<term> (the picker's search box) the matching customers in the employee's branch
    scope are put on g.picker_results for the picker to list."""
    from flask import g

    from .models import Account, Customer
    cid = request.values.get("customer", type=int)
    if cid:
        return ensure_branch_access(db.session.get(Customer, cid))
    term = request.args.get("find", "").strip()[:80]
    if term:
        from .services import search_customers
        g.picker_term = term
        g.picker_results = search_customers(term, branch_scope(Customer.query, Customer))             .order_by(Customer.name).limit(15).all()
        audit("Customer search", "customer_profile", details=f"picker, term length {len(term)}")
        db.session.commit()
        return None
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
    (r".{12,}", "at least 12 characters"),
    (r"[A-Z]", "an uppercase letter"),
    (r"[a-z]", "a lowercase letter"),
    (r"\d", "a digit"),
    (r"[^A-Za-z0-9]", "a special character"),
]


COMMON_WORDS = ("password", "passw0rd", "welcome", "qwerty", "admin", "letmein", "banking", "123456",
                "abc123", "iloveyou", "monkey", "dragon", "local bank", "localbank")


def password_problems(password, email=None):
    problems = [msg for pattern, msg in PASSWORD_RULES if not re.search(pattern, password or "")]
    low = (password or "").lower()
    if any(w in low for w in COMMON_WORDS):
        problems.append("no common words such as 'password' or 'welcome'")
    if email and len(email.split("@")[0]) >= 4 and email.split("@")[0].lower() in low:
        problems.append("not your email name")
    return problems


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
    """The connecting client's address. X-Forwarded-For is honoured only from proxies listed
    as trusted (ProxyFix, BANK_TRUSTED_PROXIES), so a client cannot forge its IP."""
    return (request.remote_addr or "")[:64]


def audit(action, module=None, reference=None, details=None, employee=None, result="success"):
    emp = employee or _actor()
    db.session.add(AuditLog(
        employee_id=emp.id if emp else None,
        email=emp.email if emp else None,
        action=action, module=module, reference=(reference or "")[:80] or None,
        details=(details or "")[:500], ip_address=client_ip() if has_request_context() else "system",
        result=result,
    ))


def _actor():
    """The signed-in employee, or None for background jobs (no request)."""
    if has_request_context() and current_user and current_user.is_authenticated:
        return current_user
    return None


def activity(customer_id, action, reference=None, details=None):
    emp = _actor()
    db.session.add(Activity(customer_id=customer_id, employee_id=emp.id if emp else None,
                            action=action, reference=reference, details=details))


# --------------------------------------------------------------------------
# One-time form tokens (duplicate-submission protection)
# --------------------------------------------------------------------------

def form_nonce():
    return secrets.token_urlsafe(24)


def consume_form_token():
    """Record the form's one-time token. False if it is missing or was already used.
    The token is the primary key of form_token, so two concurrent submissions of the same
    form cannot both succeed: the second insert waits for the first and then fails."""
    from sqlalchemy.exc import IntegrityError
    from .models import FormToken
    token = request.form.get("_idem", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        return False
    try:
        with db.session.begin_nested():
            db.session.add(FormToken(token=token))
        return True
    except IntegrityError:
        return False


def once_per_form(fn):
    """Route decorator for state-changing POSTs that must never run twice."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == "POST" and not consume_form_token():
            db.session.rollback()
            flash("This form was already submitted, so it was not processed again. "
                  "Check the record before trying again.", "warning")
            return redirect(request.url)
        return fn(*args, **kwargs)
    return wrapper
