from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..charges import charge_fee, fee_amount, waive_problem
from ..extensions import db
from ..models import FEE_STATUSES, Account, FeeCharge, FeeType
from ..money import ZERO, parse_money, parse_rate
from ..security import audit, branch_scope, ensure_branch_access, once_per_form, require_perm
from ..services import create_approval, pending_request
from ..utils import inr
from .transactions import _receipts_from_request

bp = Blueprint("fees", __name__, url_prefix="/fees")


@bp.route("/")
@login_required
@require_perm("transactions", "view")
def index():
    status = request.args.get("status", "")
    q = branch_scope(FeeCharge.query, FeeCharge)
    if status in FEE_STATUSES:
        q = q.filter(FeeCharge.status == status)
    counts = dict(branch_scope(FeeCharge.query, FeeCharge).with_entities(FeeCharge.status, db.func.count())
                  .group_by(FeeCharge.status).all())
    due_total = (branch_scope(FeeCharge.query, FeeCharge).filter(FeeCharge.status == "Due")
                 .with_entities(db.func.coalesce(db.func.sum(FeeCharge.total), 0)).scalar())
    page = request.args.get("page", 1, type=int)
    charges = q.order_by(FeeCharge.id.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template("fees/index.html", charges=charges, status=status, counts=counts, due_total=due_total,
                           waiting={c.id: pending_request("fee_waiver", c.id) for c in charges.items
                                    if c.status in ("Charged", "Due")})


@bp.route("/charge", methods=["GET", "POST"])
@login_required
@require_perm("transactions", "create")
@once_per_form
def charge():
    """Teller charges a service fee (cheque book, duplicate statement, DD …) to an account."""
    acc_no = request.values.get("account", "").strip()[:20]
    account = Account.query.filter_by(account_number=acc_no).first() if acc_no else None
    if account and not (current_user.sees_all_branches or account.branch_id == current_user.branch_id):
        account = None  # same answer as "not found"
    types = FeeType.query.filter_by(active=True, kind="manual").order_by(FeeType.name).all()

    def show(f=None):
        if acc_no and not account:
            flash("Account not found in your branch.", "warning")
        return render_template("fees/charge.html", account=account, acc_no=acc_no, types=types, f=f or {},
                               receipts=_receipts_from_request(),
                               quotes={t.id: fee_amount(t, 0) for t in types if t.calc == "flat"})

    if request.method == "POST" and account:
        ft = db.session.get(FeeType, request.form.get("fee_type", type=int) or 0)
        if not ft or ft not in types:
            flash("Choose a fee from the list.", "danger")
            return show(request.form)
        base = None
        if ft.calc == "percent":
            base = parse_money(request.form.get("base"))
            if base is None:
                flash(f"Enter the amount the {ft.percent:g}% fee applies to (for example the draft amount).", "danger")
                return show(request.form)
        try:
            ch = charge_fee(account, ft, base=base, note=(request.form.get("note") or "").strip())
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return show(request.form)
        audit("Fee charged", "transactions", ch.charge_ref,
              f"{ft.code} {inr(ch.total, 2)} on {account.account_number} -> {ch.status}")
        db.session.commit()
        if ch.status == "Charged":
            flash(f"{ft.name}: {inr(ch.total, 2)} charged to {account.account_number} ({ch.charge_ref}). "
                  f"New balance {inr(account.balance, 2)}.", "success")
            return redirect(url_for("fees.charge", account=account.account_number, receipt=ch.txn_id))
        flash(f"The balance does not cover {inr(ch.total, 2)}, so {ft.name} was recorded as due ({ch.charge_ref}). "
              f"It is collected automatically from the next credit to the account.", "warning")
        return redirect(url_for("fees.charge", account=account.account_number))
    return show()


@bp.route("/<int:fid>/waive", methods=["POST"])
@login_required
@require_perm("transactions", "create")
@once_per_form
def waive(fid):
    """Maker asks to waive a fee; a checker and a manager must approve (charges.exec_fee_waiver)."""
    ch = ensure_branch_access(db.session.get(FeeCharge, fid))
    back = request.form.get("back") if request.form.get("back", "").startswith("/") else url_for("fees.index")
    reason = (request.form.get("reason") or "").strip()[:200]
    problem = ("Enter the reason for the waiver (at least 5 characters)." if len(reason) < 5 else waive_problem(ch))
    open_req = pending_request("fee_waiver", ch.id)
    if problem or open_req:
        flash(problem or f"A waiver of {ch.charge_ref} is already waiting for approval ({open_req.request_number}).",
              "danger" if problem else "warning")
        return redirect(back)
    req = create_approval("fee_waiver", "fee_charge", ch.id, ch.customer, ch.total,
                          f"Waive {ch.fee_type.name} {ch.charge_ref} of {inr(ch.total, 2)} on {ch.account.account_number}",
                          {"reason": reason, "charge_ref": ch.charge_ref})
    db.session.commit()
    flash(f"Waiver of {ch.charge_ref} sent for approval ({req.request_number}).", "success")
    return redirect(back)


@bp.route("/types", methods=["GET", "POST"])
@login_required
@require_perm("settings", "view")
@once_per_form
def types():
    """Fee schedule. Changes need the Settings edit permission and are audited."""
    if request.method == "POST":
        if not current_user.can("settings", "edit"):
            abort(403)
        f = request.form
        ft = db.session.get(FeeType, f.get("id", type=int) or 0) if f.get("id") else FeeType()
        if ft is None:
            abort(404)
        problems = []
        name = (f.get("name") or "").strip()[:120]
        code = (f.get("code") or "").strip().upper()[:20]
        calc, kind = f.get("calc"), f.get("kind")
        if len(name) < 3:
            problems.append("Enter a fee name (3 characters or more).")
        if not ft.id and (not code.replace("_", "").isalnum() or FeeType.query.filter_by(code=code).first()):
            problems.append("Enter a unique short code (letters and digits).")
        if calc not in ("flat", "percent") or kind not in ("manual", "min_balance"):
            problems.append("Choose how the fee is worked out and when it applies.")
        amount = parse_money(f.get("amount")) if calc == "flat" else None
        percent = parse_rate(f.get("percent"), maximum=Decimal(25)) if calc == "percent" else None
        if calc == "flat" and amount is None:
            problems.append("Enter the fee amount.")
        if calc == "percent" and (percent is None or percent <= ZERO):
            problems.append("Enter the percentage (up to 25%).")
        lo = parse_money(f.get("min_amount")) if f.get("min_amount", "").strip() else None
        hi = parse_money(f.get("max_amount")) if f.get("max_amount", "").strip() else None
        if lo is not None and hi is not None and lo > hi:
            problems.append("The minimum cannot be above the maximum.")
        raw_gst = (f.get("gst_pct") or "0").strip()
        gst = ZERO if raw_gst in ("0", "0.0", "0.00") else parse_rate(raw_gst, maximum=Decimal(28))
        if gst is None:
            problems.append("GST must be between 0% and 28%.")
        threshold = parse_money(f.get("threshold")) if kind == "min_balance" else None
        if kind == "min_balance" and (threshold is None or calc != "flat"):
            problems.append("A minimum-balance charge needs a flat amount and the minimum balance to keep.")
        if problems:
            for p in problems:
                flash(p, "danger")
            return redirect(url_for("fees.types", edit=ft.id or ""))
        if not ft.id:
            ft.code = code
            db.session.add(ft)
        ft.name, ft.calc, ft.kind, ft.amount, ft.percent = name, calc, kind, amount, percent
        ft.min_amount, ft.max_amount, ft.gst_pct, ft.threshold = lo, hi, gst, threshold
        ft.active = bool(f.get("active"))
        db.session.flush()
        audit("Fee schedule changed", "settings", ft.code,
              f"{name}: {calc} {amount or percent} gst {gst}% {kind} {'on' if ft.active else 'off'}")
        db.session.commit()
        flash(f"Fee '{name}' saved.", "success")
        return redirect(url_for("fees.types"))
    edit = db.session.get(FeeType, request.args.get("edit", type=int) or 0)
    return render_template("fees/types.html", types=FeeType.query.order_by(FeeType.kind, FeeType.name).all(),
                           edit=edit, can_edit=current_user.can("settings", "edit"))
