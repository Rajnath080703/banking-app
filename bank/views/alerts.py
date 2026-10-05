from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import MODULES, Alert, AlertRule
from ..security import audit, require_perm
from ..services import scan_scheduled_alerts, severity_order, visible_alerts
from ..utils import parse_float

bp = Blueprint("alerts", __name__, url_prefix="/alerts")


@bp.route("/")
@login_required
@require_perm("alerts", "view")
def index():
    q = visible_alerts(current_user)
    show = request.args.get("show", "open")
    atype = request.args.get("type", "")
    counts = dict(q.filter(Alert.is_resolved.is_(False)).with_entities(Alert.alert_type, func.count())
                  .group_by(Alert.alert_type).all())
    if show == "open":
        q = q.filter(Alert.is_resolved.is_(False))
    elif show == "resolved":
        q = q.filter(Alert.is_resolved.is_(True))
    if atype:
        q = q.filter(Alert.alert_type == atype)
    page = request.args.get("page", 1, type=int)
    alerts = q.order_by(Alert.is_resolved, severity_order(), Alert.created_at.desc()) \
        .paginate(page=page, per_page=25, error_out=False)
    return render_template("alerts/index.html", alerts=alerts, show=show, atype=atype, counts=counts,
                           types=Alert.TYPE_LABELS)


def _get_visible(aid):
    alert = visible_alerts(current_user).filter(Alert.id == aid).first()
    if not alert:
        abort(404)
    return alert


@bp.route("/<int:aid>/open")
@login_required
@require_perm("alerts", "view")
def open_alert(aid):
    alert = _get_visible(aid)
    alert.is_read = True
    db.session.commit()
    return redirect(alert.link or url_for("alerts.index"))


@bp.route("/<int:aid>/resolve", methods=["POST"])
@login_required
@require_perm("alerts", "edit")
def resolve(aid):
    alert = _get_visible(aid)
    alert.is_resolved = True
    alert.is_read = True
    alert.resolved_by = current_user.id
    alert.resolved_at = datetime.utcnow()
    audit("Alert resolved", "alerts", alert.reference, alert.title)
    db.session.commit()
    flash("Alert resolved.", "success")
    return redirect(request.referrer or url_for("alerts.index"))


@bp.route("/mark-read", methods=["POST"])
@login_required
@require_perm("alerts", "view")
def mark_read():
    ids = [a.id for a in visible_alerts(current_user).filter_by(is_read=False).all()]
    if ids:
        Alert.query.filter(Alert.id.in_(ids)).update({"is_read": True}, synchronize_session=False)
        db.session.commit()
    return redirect(request.referrer or url_for("alerts.index"))


@bp.route("/scan", methods=["POST"])
@login_required
@require_perm("alerts", "edit")
def scan():
    scan_scheduled_alerts(force=True)
    audit("Alert scan run", "alerts")
    db.session.commit()
    flash("Alert scan complete.", "success")
    return redirect(url_for("alerts.index"))


@bp.route("/rules", methods=["GET", "POST"])
@login_required
@require_perm("alerts", "view")
def rules():
    if request.method == "POST":
        if not current_user.can("settings", "edit"):
            abort(403)
        valid_modules = {m for m, _ in MODULES}
        for rule in AlertRule.query.all():
            prefix = f"r{rule.id}_"
            rule.enabled = bool(request.form.get(prefix + "enabled"))
            rule.threshold = parse_float(request.form.get(prefix + "threshold"), rule.threshold)
            rule.window_minutes = request.form.get(prefix + "window", type=int) or rule.window_minutes
            sev = request.form.get(prefix + "severity")
            if sev in ("Low", "Medium", "High", "Critical"):
                rule.severity = sev
            mod = request.form.get(prefix + "route")
            if mod in valid_modules:
                rule.route_module = mod
        audit("Alert rules updated", "settings")
        db.session.commit()
        flash("Risk rules saved.", "success")
        return redirect(url_for("alerts.rules"))
    return render_template("alerts/rules.html", rules=AlertRule.query.all(), modules=MODULES)
