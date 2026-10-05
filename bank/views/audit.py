from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from ..extensions import db
from ..models import AuditLog, Employee
from ..security import require_perm
from ..utils import date_range_from_request, day_bounds

bp = Blueprint("audit", __name__, url_prefix="/audit")

CATEGORIES = {
    "auth": ["Login", "Logout", "Failed login", "Account locked", "Session timeout"],
    "access": ["Customer profile viewed", "Sensitive data revealed", "Document accessed",
               "Document file downloaded", "Access denied", "Cross-branch access denied"],
    "changes": ["Customer profile updated", "Customer created", "Account closure requested",
                "Transaction posted", "Loan created", "Loan repayment posted"],
    "approvals": ["Approval request created", "Approval reviewed (checker)", "Approval approved",
                  "Approval rejected"],
    "kyc": ["Document uploaded", "Document verified", "Document rejected"],
    "admin": ["Employee created", "Employee updated", "Role created", "Permissions updated",
              "Settings updated", "Alert rules updated", "Password reset", "Sessions revoked",
              "MFA enabled", "MFA disabled", "Password changed"],
    "exports": ["Report exported"],
}


@bp.route("/")
@login_required
@require_perm("audit_logs", "view")
def index():
    start, end, rkey = date_range_from_request("week")
    s, e = day_bounds(start, end)
    q = AuditLog.query.filter(AuditLog.timestamp.between(s, e))
    if not current_user.sees_all_branches:
        q = q.join(Employee, AuditLog.employee_id == Employee.id).filter(Employee.branch_id == current_user.branch_id)
    cat = request.args.get("cat", "")
    if cat in CATEGORIES:
        q = q.filter(AuditLog.action.in_(CATEGORIES[cat]))
    user = request.args.get("user", "").strip()
    if user:
        q = q.filter(AuditLog.email.ilike(f"%{user}%"))
    ref = request.args.get("ref", "").strip()
    if ref:
        q = q.filter(db.or_(AuditLog.reference.ilike(f"%{ref}%"), AuditLog.action.ilike(f"%{ref}%")))
    page = request.args.get("page", 1, type=int)
    logs = q.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=40, error_out=False)
    return render_template("audit/index.html", logs=logs, start=start, end=end, rkey=rkey, cat=cat,
                           user=user, ref=ref, categories=CATEGORIES)
