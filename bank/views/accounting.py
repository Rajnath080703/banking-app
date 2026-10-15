from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from ..extensions import db
from ..ledger import reconciliation, trial_balance
from ..models import Branch, InterestRun, JournalEntry, JournalLine
from ..money import ZERO
from ..security import audit, once_per_form, require_perm
from ..services import create_approval, employee_branch_ids, savings_interest_preview
from ..utils import local_today, parse_date, inr

bp = Blueprint("accounting", __name__, url_prefix="/accounting")


def _scope():
    """Branch scope for ledger views: own branch, or a chosen/all branches for head office."""
    if current_user.sees_all_branches:
        b = request.args.get("branch", type=int)
        return [b] if b else None
    return [current_user.branch_id]


@bp.route("/")
@login_required
@require_perm("accounting", "view")
def index():
    scope = _scope()
    tb = trial_balance(scope)
    checks = reconciliation(scope)
    totals = {"debit": sum((r["debit"] for r in tb), ZERO), "credit": sum((r["credit"] for r in tb), ZERO)}
    audit("Trial balance viewed", "accounting")
    db.session.commit()
    return render_template("accounting/index.html", tb=tb, totals=totals, checks=checks,
                           branches=Branch.query.all() if current_user.sees_all_branches else [],
                           selected_branch=request.args.get("branch", type=int))


@bp.route("/journal")
@login_required
@require_perm("accounting", "view")
def journal():
    q = JournalEntry.query.options(selectinload(JournalEntry.lines))
    scope = _scope()
    if scope is not None:
        q = q.filter(JournalEntry.branch_id.in_(scope))
    ref = request.args.get("ref", "").strip()[:40]
    if ref:
        q = q.filter(db.or_(JournalEntry.entry_ref == ref.upper(), JournalEntry.source_ref == ref.upper(),
                            JournalEntry.source_ref == ref))
    gl = request.args.get("gl", "").strip()
    if gl:
        q = q.filter(JournalEntry.id.in_(db.session.query(JournalLine.entry_id).filter(JournalLine.gl_code == gl)))
    page = request.args.get("page", 1, type=int)
    entries = q.order_by(JournalEntry.id.desc()).paginate(page=page, per_page=30, error_out=False)
    from ..models import GLAccount
    return render_template("accounting/journal.html", entries=entries, ref=ref, gl=gl,
                           accounts=GLAccount.query.order_by(GLAccount.code).all())


def _default_quarter():
    """The last completed calendar quarter."""
    from datetime import timedelta
    today = local_today()
    this_q_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    prev_end = this_q_start - timedelta(days=1)
    return date(prev_end.year, ((prev_end.month - 1) // 3) * 3 + 1, 1), prev_end


@bp.route("/interest", methods=["GET", "POST"])
@login_required
@require_perm("interest", "view")
@once_per_form
def interest():
    d_start, d_end = _default_quarter()
    start = parse_date(request.values.get("start")) or d_start
    end = parse_date(request.values.get("end")) or d_end
    branch_ids = employee_branch_ids(current_user)
    problem = None
    if end < start:
        problem = "The period end is before its start."
    elif end >= local_today():
        problem = "Interest can only be posted for a period that has already ended."
    elif (end - start).days > 366:
        problem = "The period can be at most one year."
    rows = [] if problem else savings_interest_preview(start, end, branch_ids)
    total = sum((r["interest"] for r in rows), ZERO)
    already = InterestRun.query.filter(InterestRun.branch_id.in_(branch_ids), InterestRun.period_start == start,
                                       InterestRun.period_end == end).all()
    if request.method == "POST":
        if not current_user.can("interest", "create"):
            flash("Your role can preview interest but not submit a posting.", "danger")
        elif problem or already or not rows:
            flash(problem or ("Interest for this period was already posted." if already else "Nothing to post."), "danger")
        else:
            req = create_approval("interest_posting", "interest", None, None, total,
                                  f"Savings interest {start:%d %b %Y} – {end:%d %b %Y}: {inr(total, 2)} on {len(rows)} accounts",
                                  {"start": start.isoformat(), "end": end.isoformat(), "branch_ids": branch_ids,
                                   "total": str(total), "accounts": len(rows)}, branch_id=current_user.branch_id)
            db.session.commit()
            flash(f"Interest posting sent for maker-checker approval ({req.request_number}). Amounts are "
                  f"recalculated at approval time.", "success")
            return redirect(url_for("approvals.detail", rid=req.id))
    runs = InterestRun.query.filter(InterestRun.branch_id.in_(branch_ids)).order_by(InterestRun.posted_at.desc()).limit(12).all()
    return render_template("accounting/interest.html", start=start, end=end, rows=rows[:200], count=len(rows),
                           total=total, problem=problem, already=already, runs=runs)
