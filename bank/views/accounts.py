from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Account, ApprovalRequest, Customer
from ..security import customer_from_request, audit, branch_scope, ensure_branch_access, require_perm
from ..services import create_approval, search_customers
from ..utils import unique_number

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


@bp.route("/")
@login_required
@require_perm("customer_accounts", "view")
def index():
    q = branch_scope(Account.query, Account).join(Customer)
    status = request.args.get("status", "")
    acc_type = request.args.get("type", "")
    term = request.args.get("q", "").strip()
    if status:
        q = q.filter(Account.status == status)
    if acc_type:
        q = q.filter(Account.account_type == acc_type)
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(Account.customer_id.in_(ids))
    page = request.args.get("page", 1, type=int)
    order = Account.balance.desc() if request.args.get("sort") == "balance" else Account.opened_on.desc()
    accounts = q.order_by(order).paginate(page=page, per_page=25, error_out=False)
    return render_template("accounts/index.html", accounts=accounts, status=status, acc_type=acc_type, term=term)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("customer_accounts", "create")
def new():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        acc_type = request.form.get("account_type")
        if acc_type not in ("Savings", "Current"):
            flash("Choose an account type.", "danger")
            return redirect(request.url)
        acc = Account(account_number=unique_number(Account, "account_number", "50", 10),
                      customer_id=customer.id, branch_id=customer.branch_id, account_type=acc_type,
                      interest_rate=3.0 if acc_type == "Savings" else 0, balance=0, status="Pending")
        db.session.add(acc)
        db.session.flush()
        req = create_approval("account_open", "account", acc.id, customer, None,
                              f"Open {acc_type} account {acc.account_number} for {customer.name}")
        db.session.commit()
        flash(f"Account {acc.account_number} submitted for approval ({req.request_number}).", "success")
        return redirect(url_for("customers.profile", cid=customer.id, tab="accounts"))
    return render_template("accounts/new.html", customer=customer)


@bp.route("/<int:aid>/close", methods=["POST"])
@login_required
@require_perm("customer_accounts", "edit")
def close(aid):
    acc = ensure_branch_access(db.session.get(Account, aid))
    if acc.status == "Closed":
        flash("Account is already closed.", "warning")
    elif ApprovalRequest.query.filter_by(request_type="account_close", ref_id=acc.id) \
            .filter(ApprovalRequest.status.in_(["Pending Review", "Reviewed"])).first():
        flash("A closure request for this account is already pending.", "warning")
    else:
        reason = request.form.get("reason", "").strip() or "Customer request"
        req = create_approval("account_close", "account", acc.id, acc.customer, acc.balance,
                              f"Close {acc.account_type} account {acc.account_number} — {reason}",
                              {"reason": reason})
        audit("Account closure requested", "customer_accounts", acc.account_number, reason)
        db.session.commit()
        flash(f"Closure request {req.request_number} sent for maker-checker approval.", "success")
    return redirect(url_for("customers.profile", cid=acc.customer_id, tab="accounts"))
