"""What is waiting for the signed-in employee: approvals they may review or decide, and KYC
documents they may verify. Built on the same maker-checker rules the actions enforce, so a
count or a "next item" never points at something the employee would then be refused."""
from flask import g
from flask_login import current_user

from .models import ApprovalRequest, Customer, Document
from .security import branch_scope
from .services import APPROVAL_MODULE, can_decide, can_review

SCAN_LIMIT = 500  # open requests examined per page view; queues beyond this are a reporting problem


def _open_requests():
    modules = sorted({m for m in APPROVAL_MODULE.values() if current_user.can(m, "approve")})
    if not modules:
        return []
    q = branch_scope(ApprovalRequest.query, ApprovalRequest).filter(
        ApprovalRequest.module.in_(modules), ApprovalRequest.status.in_(["Pending Review", "Reviewed"]),
        ApprovalRequest.maker_id != current_user.id)
    return q.order_by(ApprovalRequest.created_at).limit(SCAN_LIMIT).all()


def approvals_awaiting_me():
    """Oldest first, so the queue is worked in the order requests arrived."""
    if "wq_approvals" not in g:
        g.wq_approvals = [r for r in _open_requests() if can_review(r, current_user) or can_decide(r, current_user)]
    return g.wq_approvals


def next_approval(after_id=None):
    return next((r for r in approvals_awaiting_me() if r.id != after_id), None)


def kyc_awaiting_me():
    """Pending documents this employee may verify (never their own uploads)."""
    if not (current_user.can("kyc", "approve") or current_user.can("kyc", "reject")):
        return Document.query.filter(False)
    q = branch_scope(Document.query.join(Customer), Customer)
    return q.filter(Document.status == "Pending",
                    (Document.uploaded_by != current_user.id) | (Document.uploaded_by.is_(None))) \
        .order_by(Document.uploaded_at)


def next_kyc(after_id=None):
    q = kyc_awaiting_me()
    if after_id:
        q = q.filter(Document.id != after_id)
    return q.first()


def counts():
    return {"approvals": len(approvals_awaiting_me()), "kyc": kyc_awaiting_me().count()}
