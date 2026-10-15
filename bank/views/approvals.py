from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import workqueue
from ..extensions import db
from ..models import ApprovalRequest
from ..security import branch_scope, ensure_branch_access, once_per_form
from ..services import (APPROVAL_MODULE, ApprovalBlocked, approve_approval, can_decide, can_reject,
                        can_review, lock, reject_approval, review_approval)

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
    awaiting = workqueue.approvals_awaiting_me()
    can_act = any(current_user.can(m, "approve") for m in modules)
    status = request.args.get("status", "awaiting" if can_act else "open")
    if status == "awaiting":
        q = q.filter(ApprovalRequest.id.in_([r.id for r in awaiting] or [0]))
    elif status == "open":
        q = q.filter(ApprovalRequest.status.in_(["Pending Review", "Reviewed"]))
    elif status == "mine":
        q = q.filter(ApprovalRequest.maker_id == current_user.id)
    elif status in ("Approved", "Rejected"):
        q = q.filter(ApprovalRequest.status == status)
    rtype = request.args.get("type", "")
    if rtype:
        q = q.filter(ApprovalRequest.request_type == rtype)
    page = request.args.get("page", 1, type=int)
    order = ApprovalRequest.created_at.asc() if status == "awaiting" else ApprovalRequest.created_at.desc()
    reqs = q.order_by(order).paginate(page=page, per_page=25, error_out=False)
    actionable = {r.id: can_review(r, current_user) or can_decide(r, current_user) for r in reqs.items}
    return render_template("approvals/index.html", reqs=reqs, status=status, rtype=rtype,
                           actionable=actionable, types=ApprovalRequest.TYPE_LABELS, can_act=can_act,
                           awaiting_count=len(awaiting))


@bp.route("/<int:rid>", methods=["GET", "POST"])
@login_required
@once_per_form
def detail(rid):
    req = ensure_branch_access(db.session.get(ApprovalRequest, rid))
    if not current_user.can(req.module, "view"):
        abort(403)
    if request.method == "POST":
        # Lock the request row: a second click or a second approver waits here, then sees the
        # new status and is refused, so an approval can never execute twice.
        lock(req)
        if req.status in ("Approved", "Rejected"):
            db.session.rollback()
            who = req.approver.name if req.approver else "another officer"
            flash(f"This request was already {req.status.lower()} by {who}. Nothing was changed.", "warning")
            return _to_next(rid)
        action = request.form.get("action")
        remarks = request.form.get("remarks", "").strip()
        if action in ("reject",) and not remarks:
            flash("Remarks are required when rejecting.", "danger")
            return redirect(url_for("approvals.detail", rid=rid))
        if action == "review":
            review_approval(req, remarks)
            msg = f"{req.request_number} reviewed and forwarded for final approval."
        elif action == "approve":
            try:
                approve_approval(req, remarks)
            except ApprovalBlocked as exc:
                db.session.rollback()
                flash(f"Not approved: {exc}", "danger")
                return redirect(url_for("approvals.detail", rid=rid))
            msg = f"{req.request_number} approved and executed."
        elif action == "reject":
            reject_approval(req, remarks)
            msg = f"{req.request_number} rejected."
        else:
            abort(400)
        db.session.commit()
        flash(msg, "success")
        return _to_next(rid)
    return render_template("approvals/detail.html", r=req,
                           can_review=can_review(req, current_user),
                           can_decide=can_decide(req, current_user),
                           can_reject=can_reject(req, current_user))


def _to_next(done_id):
    """After a decision, go straight to the next request waiting for this employee."""
    g.pop("wq_approvals", None)  # the queue changed with this decision
    nxt = workqueue.next_approval(after_id=done_id)
    if nxt:
        flash(f"Next in your queue: {nxt.request_number} ({nxt.type_label}).", "info")
        return redirect(url_for("approvals.detail", rid=nxt.id))
    flash("Your approval queue is clear.", "info")
    return redirect(url_for("approvals.index"))
