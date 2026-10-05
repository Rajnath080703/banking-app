from datetime import date, timedelta

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (Account, Activity, Alert, ApprovalRequest, Branch, Customer, Document,
                      FixedDeposit, Loan, LoanRepayment, Setting, Ticket, Transaction)
from ..security import require_perm
from ..services import money_sum, scan_scheduled_alerts, severity_order, visible_alerts
from ..utils import (add_months, compare_label, date_range_from_request, day_bounds, pct_change,
                     previous_period)

bp = Blueprint("dashboard", __name__)


def _branch_ids():
    if current_user.sees_all_branches:
        b = request.args.get("branch", type=int)
        return [b] if b else [x.id for x in Branch.query.all()]
    return [current_user.branch_id]


def compute_kpis(branch_ids, start, end):
    s, e = day_bounds(start, end)
    today = date.today()
    kyc_days = Setting.get_int("kyc_expiry_warning_days")
    in_b = lambda model: model.branch_id.in_(branch_ids)  # noqa: E731
    k = {}
    k["total_customers"] = Customer.query.filter(in_b(Customer)).count()
    k["new_customers"] = Customer.query.filter(in_b(Customer), Customer.created_at.between(s, e)).count()
    k["total_deposits"] = (money_sum(Account.balance, in_b(Account), Account.status != "Closed")
                           + money_sum(FixedDeposit.principal, in_b(FixedDeposit), FixedDeposit.status == "Active"))
    k["casa"] = money_sum(Account.balance, in_b(Account), Account.status != "Closed")
    k["loans_disbursed_total"] = money_sum(Loan.amount, in_b(Loan), Loan.disbursed_on != None)  # noqa
    k["loan_outstanding"] = money_sum(Loan.outstanding, in_b(Loan), Loan.status.in_(["Active", "Overdue"]))
    k["loan_disbursement"] = money_sum(Loan.amount, in_b(Loan), Loan.disbursed_on.between(start, end))
    k["loan_collection"] = money_sum(LoanRepayment.amount,
                                     LoanRepayment.loan_id.in_(db.session.query(Loan.id).filter(in_b(Loan))),
                                     LoanRepayment.paid_on.between(start, end))
    k["pending_approvals"] = ApprovalRequest.query.filter(
        in_b(ApprovalRequest), ApprovalRequest.status.in_(["Pending Review", "Reviewed"])).count()
    k["new_accounts"] = Account.query.filter(in_b(Account), Account.opened_on.between(start, end)).count()
    k["closed_accounts"] = Account.query.filter(in_b(Account), Account.closed_on.between(start, end)).count()
    k["closed_total"] = Account.query.filter(in_b(Account), Account.status.in_(["Closed", "Inactive"])).count()
    k["kyc_pending"] = Customer.query.filter(in_b(Customer), Customer.kyc_status == "Pending").count()
    k["kyc_expiring"] = Document.query.join(Customer).filter(
        in_b(Customer), Document.category == "KYC", Document.expiry_date != None,  # noqa
        Document.expiry_date <= today + timedelta(days=kyc_days),
        Document.status != "Rejected").count()
    k["tickets_raised"] = Ticket.query.filter(in_b(Ticket), Ticket.created_at.between(s, e)).count()
    k["tickets_open"] = Ticket.query.filter(in_b(Ticket), Ticket.status.notin_(["Resolved", "Closed"])).count()
    k["overdue_loans"] = Loan.query.filter(in_b(Loan), Loan.status == "Overdue").count()
    k["overdue_amount"] = money_sum(Loan.outstanding, in_b(Loan), Loan.status == "Overdue")
    k["fd_created"] = FixedDeposit.query.filter(in_b(FixedDeposit), FixedDeposit.start_date.between(start, end)).count()
    k["fd_matured"] = FixedDeposit.query.filter(in_b(FixedDeposit), FixedDeposit.maturity_date.between(start, end)).count()
    k["income"] = money_sum(Transaction.amount, in_b(Transaction), Transaction.txn_type == "Credit",
                            Transaction.timestamp.between(s, e))
    k["outgoing"] = money_sum(Transaction.amount, in_b(Transaction), Transaction.txn_type == "Debit",
                              Transaction.timestamp.between(s, e))
    return k


def compute_charts(branch_ids, start, end):
    s, e = day_bounds(start, end)
    in_b = lambda model: model.branch_id.in_(branch_ids)  # noqa: E731
    charts = {}
    days = (end - start).days
    # Income vs outgoing + volume: daily for ranges <= 62 days, else monthly.
    fmt = "%Y-%m-%d" if days <= 62 else "%Y-%m"
    bucket = func.strftime(fmt, Transaction.timestamp)
    rows = (db.session.query(bucket, Transaction.txn_type, func.sum(Transaction.amount), func.count())
            .filter(in_b(Transaction), Transaction.timestamp.between(s, e))
            .group_by(bucket, Transaction.txn_type).all())
    labels = sorted({r[0] for r in rows})
    if days <= 62:
        labels = [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]
    inc = {r[0]: r[2] for r in rows if r[1] == "Credit"}
    out = {r[0]: r[2] for r in rows if r[1] == "Debit"}
    vol = {}
    for r in rows:
        vol[r[0]] = vol.get(r[0], 0) + r[3]
    pretty = [(lbl[8:] + "/" + lbl[5:7]) if len(lbl) == 10 else lbl for lbl in labels]
    charts["flow"] = {"labels": pretty, "income": [round(inc.get(lbl, 0)) for lbl in labels],
                      "outgoing": [round(out.get(lbl, 0)) for lbl in labels]}
    charts["volume"] = {"labels": pretty, "values": [vol.get(lbl, 0) for lbl in labels]}

    acc = dict(db.session.query(Account.status, func.count()).filter(in_b(Account))
               .group_by(Account.status).all())
    charts["accounts"] = {"labels": ["Active", "Inactive", "Dormant"],
                          "values": [acc.get("Active", 0), acc.get("Inactive", 0), acc.get("Dormant", 0)]}
    ln = dict(db.session.query(Loan.status, func.count()).filter(in_b(Loan)).group_by(Loan.status).all())
    charts["loans"] = {"labels": ["Active", "Closed", "Overdue", "Pending"],
                       "values": [ln.get(x, 0) for x in ["Active", "Closed", "Overdue", "Pending"]]}

    # Branch target vs achievement (current month: new deposits+loans vs pro-rated target)
    month_start = date.today().replace(day=1)
    targets = []
    for b in Branch.query.filter(Branch.id.in_(branch_ids)).all():
        dep = money_sum(Account.balance, Account.branch_id == b.id, Account.status != "Closed") + \
            money_sum(FixedDeposit.principal, FixedDeposit.branch_id == b.id, FixedDeposit.status == "Active")
        loans = money_sum(Loan.outstanding, Loan.branch_id == b.id, Loan.status.in_(["Active", "Overdue"]))
        new_acc = Account.query.filter(Account.branch_id == b.id, Account.opened_on >= month_start).count()
        targets.append({"name": b.name, "deposit_target": b.target_deposits, "deposit_actual": dep,
                        "loan_target": b.target_loans, "loan_actual": loans,
                        "acc_target": b.target_accounts, "acc_actual": new_acc})
    charts["targets"] = {"labels": [t["name"] for t in targets],
                         "deposit_pct": [round(t["deposit_actual"] / t["deposit_target"] * 100, 1) if t["deposit_target"] else 0 for t in targets],
                         "loan_pct": [round(t["loan_actual"] / t["loan_target"] * 100, 1) if t["loan_target"] else 0 for t in targets]}

    # 12-month deposit & loan trends (new FDs + net credits / disbursements)
    months = [add_months(date.today().replace(day=1), -i) for i in range(11, -1, -1)]
    m_labels = [m.strftime("%b %y") for m in months]
    keys = [m.strftime("%Y-%m") for m in months]
    fd_rows = dict(db.session.query(func.strftime("%Y-%m", FixedDeposit.start_date), func.sum(FixedDeposit.principal))
                   .filter(in_b(FixedDeposit)).group_by(func.strftime("%Y-%m", FixedDeposit.start_date)).all())
    cr_rows = dict(db.session.query(func.strftime("%Y-%m", Transaction.timestamp), func.sum(Transaction.amount))
                   .filter(in_b(Transaction), Transaction.txn_type == "Credit")
                   .group_by(func.strftime("%Y-%m", Transaction.timestamp)).all())
    ln_rows = dict(db.session.query(func.strftime("%Y-%m", Loan.disbursed_on), func.sum(Loan.amount))
                   .filter(in_b(Loan), Loan.disbursed_on != None)  # noqa
                   .group_by(func.strftime("%Y-%m", Loan.disbursed_on)).all())
    charts["trends"] = {"labels": m_labels,
                        "deposits": [round((fd_rows.get(k) or 0) + (cr_rows.get(k) or 0)) for k in keys],
                        "loans": [round(ln_rows.get(k) or 0) for k in keys]}
    return charts, targets


def compute_trends(branch_ids, start, end, rkey, k):
    """Percentage change against the comparison period (e.g. 'vs last month')."""
    p_start, p_end = previous_period(start, end, rkey)
    prev = compute_kpis(branch_ids, p_start, p_end)
    s, e = day_bounds(start, end)
    ps, pe = day_bounds(p_start, p_end)
    in_b = lambda model: model.branch_id.in_(branch_ids)  # noqa: E731
    today = date.today()
    warn = Setting.get_int("kyc_expiry_warning_days")

    prev_customers = Customer.query.filter(in_b(Customer), Customer.created_at < s).count()
    net_flow = k["income"] - k["outgoing"]
    fd_new = money_sum(FixedDeposit.principal, in_b(FixedDeposit), FixedDeposit.start_date.between(start, end))
    prev_deposits = k["total_deposits"] - net_flow - fd_new
    prev_disbursed = k["loans_disbursed_total"] - k["loan_disbursement"]
    appr = lambda a, b: ApprovalRequest.query.filter(in_b(ApprovalRequest), ApprovalRequest.created_at.between(a, b)).count()  # noqa: E731
    kyc_new = lambda a, b: Document.query.join(Customer).filter(  # noqa: E731
        in_b(Customer), Document.category == "KYC", Document.uploaded_at.between(a, b)).count()
    expiring_next = k["kyc_expiring"]
    expired_last = Document.query.join(Customer).filter(
        in_b(Customer), Document.category == "KYC",
        Document.expiry_date.between(today - timedelta(days=warn), today - timedelta(days=1))).count()

    t = {
        "total_customers": pct_change(k["total_customers"], prev_customers),
        "total_deposits": pct_change(k["total_deposits"], prev_deposits),
        "loans_disbursed_total": pct_change(k["loans_disbursed_total"], prev_disbursed),
        "pending_approvals": pct_change(appr(s, e), appr(ps, pe)),
        "kyc_pending": pct_change(kyc_new(s, e), kyc_new(ps, pe)),
        "kyc_expiring": pct_change(expiring_next, expired_last),
    }
    for key in ("new_accounts", "closed_accounts", "loan_disbursement", "loan_collection",
                "new_customers", "tickets_raised", "fd_created", "income"):
        t[key] = pct_change(k[key], prev[key])
    return t


@bp.route("/")
@login_required
def index():
    scan_scheduled_alerts()
    start, end, rkey = date_range_from_request("month")
    branch_ids = _branch_ids()
    kpis = compute_kpis(branch_ids, start, end)
    trends = compute_trends(branch_ids, start, end, rkey, kpis)
    charts, targets = compute_charts(branch_ids, start, end)

    # Branch target vs achievement, aggregated over the branches in view.
    tot = lambda key: sum(t[key] for t in targets)  # noqa: E731
    target_bars = [
        ("Deposits", tot("deposit_actual"), tot("deposit_target"), "success"),
        ("Loans", tot("loan_actual"), tot("loan_target"), ""),
        ("New Accounts", tot("acc_actual"), tot("acc_target"), "success"),
    ]
    top_accounts = (Account.query.filter(Account.branch_id.in_(branch_ids), Account.status != "Closed")
                    .order_by(Account.balance.desc()).limit(5).all()) if current_user.can("customer_accounts") else []
    recent = (Activity.query.join(Customer).filter(Customer.branch_id.in_(branch_ids), Activity.employee_id != None)  # noqa
              .order_by(Activity.timestamp.desc()).limit(6).all()) if current_user.can("customer_profile") else []
    alerts = (visible_alerts(current_user).filter_by(is_resolved=False)
              .order_by(severity_order(), Alert.created_at.desc()).limit(5).all())
    branches = Branch.query.all() if current_user.sees_all_branches else []
    return render_template("dashboard/index.html", k=kpis, tr=trends, charts=charts, targets=targets,
                           target_bars=target_bars, top_accounts=top_accounts, recent=recent, alerts=alerts,
                           start=start, end=end, rkey=rkey, cmp=compare_label(rkey),
                           branches=branches, selected_branch=request.args.get("branch", type=int))


@bp.route("/branch")
@login_required
@require_perm("branch_management", "view")
def branch():
    start, end, rkey = date_range_from_request("month")
    if current_user.sees_all_branches:
        branch_id = request.args.get("branch", type=int) or current_user.branch_id or Branch.query.first().id
    else:
        branch_id = current_user.branch_id
    b = db.session.get(Branch, branch_id)
    kpis = compute_kpis([branch_id], start, end)
    charts, targets = compute_charts([branch_id], start, end)
    branches = Branch.query.all() if current_user.sees_all_branches else []
    return render_template("dashboard/branch.html", b=b, k=kpis, charts=charts, t=targets[0] if targets else None,
                           start=start, end=end, rkey=rkey, branches=branches)
