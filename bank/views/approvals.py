from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ApprovalRequest
from ..security import branch_scope, ensure_branch_access
from ..services import (APPROVAL_MODULE, approve_approval, can_decide, can_reject, can_review,
                        reject_approval, review_approval)

bp = Blueprint("approvals", __name__, url_prefix="/approvals")


def _allowed_modules():
    return sorted({m for m in APPROVAL_MODULE.values() if current_user.can(m, "view")})


@bp.route("/")
@login_required
def index():
    modules = _allowed_modules()
    if not modules:
        abort(403)
    q = branch_scope(ApprovalRequest.query, ApprovalRequest).filter(ApprovalRequest.module.in_(modules))
    status = request.args.get("status", "open")
    if status == "open":
        q = q.filter(ApprovalRequest.status.in_(["Pending Review", "Reviewed"]))
    elif status == "mine":
        q = q.filter(ApprovalRequest.maker_id == current_user.id)
    elif status in ("Approved", "Rejected"):
        q = q.filter(ApprovalRequest.status == status)
    rtype = request.args.get("type", "")
    if rtype:
        q = q.filter(ApprovalRequest.request_type == rtype)
    page = request.args.get("page", 1, type=int)
    reqs = q.order_by(ApprovalRequest.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    actionable = {r.id: can_review(r, current_user) or can_decide(r, current_user) for r in reqs.items}
    return render_template("approvals/index.html", reqs=reqs, status=status, rtype=rtype,
                           actionable=actionable, types=ApprovalRequest.TYPE_LABELS)


@bp.route("/<int:rid>", methods=["GET", "POST"])
@login_required
def detail(rid):
    req = ensure_branch_access(db.session.get(ApprovalRequest, rid))
    if not current_user.can(req.module, "view"):
        abort(403)
    if request.method == "POST":
        action = request.form.get("action")
        remarks = request.form.get("remarks", "").strip()
        if action in ("reject",) and not remarks:
            flash("Remarks are required when rejecting.", "danger")
            return redirect(url_for("approvals.detail", rid=rid))
        if action == "review":
            review_approval(req, remarks)
            msg = "Request reviewed and forwarded to the manager for final approval."
        elif action == "approve":
            approve_approval(req, remarks)
            msg = "Request approved and executed."
        elif action == "reject":
            reject_approval(req, remarks)
            msg = "Request rejected."
        else:
            abort(400)
        db.session.commit()
        flash(msg, "success")
        return redirect(url_for("approvals.detail", rid=rid))
    return render_template("approvals/detail.html", r=req,
                           can_review=can_review(req, current_user),
                           can_decide=can_decide(req, current_user),
                           can_reject=can_reject(req, current_user))
