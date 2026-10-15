from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from ..extensions import db
from ..models import Account, ApprovalRequest, Customer
from ..security import once_per_form, customer_from_request, audit, branch_scope, ensure_branch_access, require_perm
from ..services import (ACCOUNT_TYPES, account_open_problem, closure_problem, create_approval, lock, pending_request,
                        search_customers)
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
@once_per_form
def new():
    """Open an account for an existing customer (entry points: Accounts → Open Account, and
    Customer 360 → Open new account). Creates a Pending account and an account_open request."""
    customer = customer_from_request()

    def show(error=None, f=None):
        if error:
            flash(error, "danger")
        return render_template("accounts/new.html", customer=customer, f=f or {}, types=ACCOUNT_TYPES,
                               waiting=_waiting_requests(customer))

    if request.method == "POST" and customer:
        acc_type = request.form.get("account_type")
        lock(customer)  # two employees submitting at once queue here, so only one request per type is raised
        problem = account_open_problem(customer, acc_type)
        if problem:
            return show(problem, request.form)
        acc = Account(account_number=unique_number(Account, "account_number", "50", 10),
                      customer_id=customer.id, branch_id=customer.branch_id, account_type=acc_type,
                      interest_rate=3.0 if acc_type == "Savings" else 0, balance=0, status="Pending")
        db.session.add(acc)
        db.session.flush()
        req = create_approval("account_open", "account", acc.id, customer, None,
                              f"Open {acc_type} account {acc.account_number} for {customer.name}")
        db.session.commit()
        flash(f"{acc_type} account {acc.account_number} for {customer.name} is pending approval ({req.request_number}). "
              f"It can be used once a checker reviews it and a manager approves it.", "success")
        return redirect(url_for("customers.profile", cid=customer.id, tab="accounts"))
    return show()


def _waiting_requests(customer):
    """Account-opening requests for this customer still waiting for approval, to show up front."""
    if not customer:
        return []
    out = []
    for acc in customer.accounts.filter_by(status="Pending"):
        req = pending_request("account_open", acc.id)
        if req:
            out.append((acc, req))
    return out


@bp.route("/<int:aid>/close", methods=["POST"])
@login_required
@require_perm("customer_accounts", "edit")
@once_per_form
def close(aid):
    acc = ensure_branch_access(db.session.get(Account, aid))
    problem = closure_problem(acc) if acc.status != "Closed" else None
    if acc.status == "Closed":
        flash("Account is already closed.", "warning")
    elif problem:
        flash(problem, "danger")
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


@bp.route("/<int:aid>/freeze", methods=["POST"])
@login_required
@require_perm("customer_accounts", "edit")
@once_per_form
def freeze(aid):
    """Freeze (block debits) or unfreeze an account — always via maker-checker."""
    acc = ensure_branch_access(db.session.get(Account, aid))
    reason = (request.form.get("reason") or "").strip()[:200]
    kind = "account_unfreeze" if acc.status == "Frozen" else "account_freeze"
    if acc.status in ("Closed", "Pending", "Rejected"):
        flash("Only an open account can be frozen or unfrozen.", "danger")
    elif not reason:
        flash("Give a reason for the request.", "danger")
    elif pending_request(kind, acc.id):
        flash("A request for this account is already waiting for approval.", "warning")
    else:
        verb = "Unfreeze" if kind == "account_unfreeze" else "Freeze"
        req = create_approval(kind, "account", acc.id, acc.customer, None,
                              f"{verb} account {acc.account_number} — {reason}", {"reason": reason})
        audit(f"Account {verb.lower()} requested", "customer_accounts", acc.account_number, reason)
        db.session.commit()
        flash(f"{verb} request {req.request_number} sent for maker-checker approval.", "success")
    return redirect(url_for("customers.profile", cid=acc.customer_id, tab="accounts"))
