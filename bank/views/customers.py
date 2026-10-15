import io
import re
from html import escape
from datetime import date, datetime, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template, request, send_file, session,
                   url_for)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (Account, Activity, ApprovalRequest, Branch, Customer, Document,
                      FixedDeposit, Loan, LoanRepayment, Setting, Ticket, Transaction)
from ..security import (activity, audit, branch_scope, can_view_sensitive, ensure_branch_access,
                        once_per_form, require_perm)
from ..services import money_sum, search_customers
from ..utils import local_today, parse_date, to_local, unique_number, inr

bp = Blueprint("customers", __name__, url_prefix="/customers")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

TABS = [
    ("overview", "Overview", "customer_profile"),
    ("accounts", "Accounts", "customer_accounts"),
    ("loans", "Loans", "loans"),
    ("deposits", "Deposits & FD", "deposits"),
    ("transactions", "Transactions", "transactions"),
    ("documents", "Documents", "kyc"),
    ("timeline", "Activity Timeline", "customer_profile"),
    ("repayments", "Repayments", "loans"),
    ("approvals", "Approvals", "customer_profile"),
    ("tickets", "Tickets", "tickets"),
    ("notifications", "Notifications", "alerts"),
]


def attention_items(c):
    """Open work for this customer that the logged-in employee can act on (or should know about).
    Each item: (tone, icon, text, link-or-None, link label)."""
    items = []
    today = local_today()
    warn = Setting.get_int("kyc_expiry_warning_days")

    if current_user.can("kyc"):
        pending_docs = c.documents.filter_by(status="Pending").count()
        if pending_docs:
            can_verify = current_user.can("kyc", "approve")
            items.append(("warning", "shield",
                          f"{pending_docs} document{'s' if pending_docs > 1 else ''} awaiting verification",
                          url_for("customers.profile", cid=c.id, tab="documents"), "Verify" if can_verify else "View"))
        renew = c.documents.filter(Document.category == "KYC", Document.status != "Rejected",
                                   Document.expiry_date != None,  # noqa: E711
                                   Document.expiry_date <= today + timedelta(days=warn)).all()
        for d in renew:
            expired = d.expiry_date < today
            items.append(("danger" if expired else "warning", "calendar",
                          f"{d.doc_type} {'expired' if expired else 'expires'} on {d.expiry_date:%d %b %Y} — collect a renewed copy",
                          url_for("kyc.upload", customer=c.id) if current_user.can("kyc", "create") else None, "Upload"))
        if c.kyc_status == "Pending" and not pending_docs:
            items.append(("warning", "shield", "KYC incomplete — PAN and Aadhaar must be uploaded and verified",
                          url_for("kyc.upload", customer=c.id) if current_user.can("kyc", "create") else None, "Upload"))

    if current_user.can("loans"):
        for loan in c.loans.filter(Loan.status == "Overdue").all():
            items.append(("danger", "alert",
                          f"{loan.type_label} {loan.loan_number}: EMI {loan.overdue_days} days overdue "
                          f"({inr(loan.emi)}) — follow up and record the payment",
                          url_for("loans.detail", lid=loan.id), "Collect EMI" if current_user.can("loans", "edit") else "View"))

    from ..services import can_decide, can_review
    for r in ApprovalRequest.query.filter(ApprovalRequest.customer_id == c.id,
                                          ApprovalRequest.status.in_(["Pending Review", "Reviewed"])).all():
        if not current_user.can(r.module):
            continue
        actionable = can_review(r, current_user) or can_decide(r, current_user)
        items.append(("info", "approval",
                      f"{r.type_label} {r.request_number} — {r.status.lower()}"
                      + ("" if actionable else " (waiting on another officer)"),
                      url_for("approvals.detail", rid=r.id), "Review" if actionable else "View"))

    if current_user.can("tickets"):
        open_t = c.tickets.filter(Ticket.status.notin_(["Resolved", "Closed"])).all()
        if open_t:
            mine = sum(1 for t in open_t if t.assigned_to == current_user.id)
            items.append(("info", "ticket",
                          f"{len(open_t)} open service ticket{'s' if len(open_t) > 1 else ''}"
                          + (f", {mine} assigned to you" if mine else ""),
                          url_for("customers.profile", cid=c.id, tab="tickets"), "Open"))

    if current_user.can("customer_accounts"):
        for a in c.accounts.filter(Account.status.in_(["Dormant", "Inactive"])).all():
            items.append(("warning", "wallet",
                          f"{a.account_type} account {a.account_number} is {a.status.lower()} — verify identity before "
                          f"any transaction; a deposit reactivates it",
                          url_for("customers.profile", cid=c.id, tab="accounts"), "View"))
    return items


@bp.route("/")
@login_required
@require_perm("customer_profile", "view")
def search():
    q = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    base = branch_scope(Customer.query, Customer)
    if q:
        results = search_customers(q, base).order_by(Customer.name).paginate(page=page, per_page=20, error_out=False)
        audit("Customer search", "customer_profile", details=f"term length {len(q)}")
        db.session.commit()
        if results.total == 1 and page == 1:
            return redirect(url_for("customers.profile", cid=results.items[0].id))
    else:
        results = base.order_by(Customer.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template("customers/search.html", results=results, q=q)


@bp.route("/<int:cid>")
@login_required
@require_perm("customer_profile", "view")
def profile(cid):
    c = ensure_branch_access(db.session.get(Customer, cid))
    tab = request.args.get("tab", "overview")
    tabs = [(k, label) for k, label, mod in TABS if current_user.can(mod, "view")]
    if tab not in dict(tabs):
        tab = "overview"
    page = request.args.get("page", 1, type=int)

    summary = {
        "casa": money_sum(Account.balance, Account.customer_id == c.id, Account.status != "Closed"),
        "fd": money_sum(FixedDeposit.principal, FixedDeposit.customer_id == c.id, FixedDeposit.status == "Active"),
        "loan_outstanding": money_sum(Loan.outstanding, Loan.customer_id == c.id, Loan.status.in_(["Active", "Overdue"])),
        "accounts": c.accounts.filter(Account.status != "Closed").count(),
        "loans": c.loans.filter(Loan.status.in_(["Active", "Overdue"])).count(),
        "overdue": c.loans.filter(Loan.status == "Overdue").count(),
        "open_tickets": c.tickets.filter(Ticket.status.notin_(["Resolved", "Closed"])).count(),
        "emi_total": money_sum(Loan.emi, Loan.customer_id == c.id, Loan.status.in_(["Active", "Overdue"])),
        "interest_paid": money_sum(LoanRepayment.interest_component,
                                   LoanRepayment.loan_id.in_(db.session.query(Loan.id).filter(Loan.customer_id == c.id))),
        "fd_interest": sum(fd.maturity_amount - fd.principal for fd in c.deposits.filter(FixedDeposit.status == "Active")),
    }
    data = {}
    if tab == "overview":
        data["accounts"] = c.accounts.order_by(Account.opened_on.desc()).all() if current_user.can("customer_accounts") else []
        data["loans"] = c.loans.order_by(Loan.created_at.desc()).limit(5).all() if current_user.can("loans") else []
        data["fds"] = c.deposits.filter(FixedDeposit.status.in_(["Active", "Matured", "Pending"])) \
            .order_by(FixedDeposit.start_date.desc()).all() if current_user.can("deposits") else []
        data["timeline"] = Activity.query.filter_by(customer_id=c.id).order_by(Activity.timestamp.desc()).limit(6).all()
        data["attention"] = attention_items(c)
        data["primary_account"] = c.accounts.filter(Account.status.in_(["Active", "Inactive", "Dormant"])) \
            .order_by(Account.opened_on).first()
    elif tab == "accounts":
        data["accounts"] = c.accounts.order_by(Account.opened_on.desc()).all()
    elif tab == "deposits":
        data["deposits"] = c.deposits.order_by(FixedDeposit.start_date.desc()).all()
    elif tab == "transactions":
        tq = Transaction.query.filter_by(customer_id=c.id)
        acc = request.args.get("account", type=int)
        if acc:
            tq = tq.filter_by(account_id=acc)
        data["txns"] = tq.order_by(Transaction.timestamp.desc()).paginate(page=page, per_page=25, error_out=False)
        data["accounts"] = c.accounts.all()
    elif tab == "loans":
        data["loans"] = c.loans.order_by(Loan.created_at.desc()).all()
    elif tab == "repayments":
        data["repayments"] = (LoanRepayment.query.join(Loan).filter(Loan.customer_id == c.id)
                              .order_by(LoanRepayment.paid_on.desc())
                              .paginate(page=page, per_page=25, error_out=False))
    elif tab == "documents":
        data["documents"] = c.documents.order_by(Document.uploaded_at.desc()).all()
    elif tab == "timeline":
        data["timeline"] = (Activity.query.filter_by(customer_id=c.id).order_by(Activity.timestamp.desc())
                            .paginate(page=page, per_page=30, error_out=False))
    elif tab == "approvals":
        data["approvals"] = ApprovalRequest.query.filter_by(customer_id=c.id).order_by(ApprovalRequest.created_at.desc()).all()
    elif tab == "tickets":
        data["tickets"] = c.tickets.order_by(Ticket.created_at.desc()).all()
    elif tab == "notifications":
        from ..models import Alert
        from ..services import visible_alerts
        data["alerts"] = (visible_alerts(current_user).filter(Alert.customer_id == c.id)
                          .order_by(Alert.is_resolved, Alert.created_at.desc())
                          .paginate(page=page, per_page=25, error_out=False))

    audit("Customer profile viewed", "customer_profile", c.customer_code, f"tab={tab}")
    if tab == "overview":
        activity(c.id, "Customer profile viewed", c.customer_code, f"by {current_user.name}")
    db.session.commit()
    revealed = session.pop("reveal", None) == c.id and can_view_sensitive()
    return render_template("customers/profile.html", c=c, tab=tab, tabs=tabs, s=summary, data=data,
                           can_sensitive=can_view_sensitive(), revealed=revealed)


@bp.route("/<int:cid>/statement")
@login_required
@require_perm("transactions", "print")
def statement(cid):
    """Account statement PDF the branch can print or hand to the customer at the counter."""
    c = ensure_branch_access(db.session.get(Customer, cid))
    if not current_user.can("customer_profile", "view"):
        abort(403)
    end = parse_date(request.args.get("end")) or local_today()
    start = parse_date(request.args.get("start")) or end - timedelta(days=90)
    acc_id = request.args.get("account", type=int)
    accounts = c.accounts.filter(Account.status != "Pending").order_by(Account.opened_on).all()
    if acc_id:
        accounts = [a for a in accounts if a.id == acc_id]
        if not accounts:
            abort(404)

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from ..utils import day_bounds
    s, e = day_bounds(start, end)
    rs = lambda v: f"Rs. {v:,.2f}"  # noqa: E731 — reportlab base fonts have no ₹ glyph
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=f"Statement {c.customer_code}")
    story = [Paragraph(f"{escape(Setting.get('bank_name'))} — Account Statement", styles["Title"]),
             Paragraph(f"<b>{escape(c.name)}</b> &nbsp; Customer ID {c.customer_code} &nbsp; Branch: {escape(c.branch.name)}", styles["Normal"]),
             Paragraph(f"Period: {start:%d %b %Y} to {end:%d %b %Y} &nbsp; Generated {datetime.now():%d %b %Y %H:%M} "
                       f"by {escape(current_user.name)}", small), Spacer(1, 5 * mm)]
    for a in accounts:
        txns = (Transaction.query.filter(Transaction.account_id == a.id, Transaction.timestamp.between(s, e))
                .order_by(Transaction.timestamp).all())
        cr = sum(t.amount for t in txns if t.txn_type == "Credit")
        dr = sum(t.amount for t in txns if t.txn_type == "Debit")
        # Running balance derived from the ledger: work back from today's balance through any
        # later transactions to get the closing balance of the period, then the opening balance.
        later = (Transaction.query.filter(Transaction.account_id == a.id, Transaction.timestamp > e).all())
        closing = a.balance - sum(t.amount if t.txn_type == "Credit" else -t.amount for t in later)
        opening = closing - cr + dr
        story.append(Paragraph(f"{a.account_type} Account {a.account_number} ({a.status})", styles["Heading3"]))
        rows = [["Date", "Reference", "Description", "Debit", "Credit", "Balance"],
                [start.strftime("%d-%m-%Y"), "", Paragraph("<b>Opening balance</b>", small), "", "", rs(opening)]]
        running = opening
        for t in txns:
            running += t.amount if t.txn_type == "Credit" else -t.amount
            rows.append([to_local(t.timestamp).strftime("%d-%m-%Y"), t.txn_ref, Paragraph(escape((t.description or "")[:60]), small),
                         rs(t.amount) if t.txn_type == "Debit" else "", rs(t.amount) if t.txn_type == "Credit" else "",
                         rs(running)])
        rows.append([end.strftime("%d-%m-%Y"), "", Paragraph(f"<b>Closing balance · {len(txns)} transactions</b>", small),
                     rs(dr), rs(cr), rs(closing)])
        table = Table(rows, colWidths=[20 * mm, 34 * mm, 54 * mm, 24 * mm, 24 * mm, 26 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6FEB")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F3F6FB")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story += [table, Spacer(1, 6 * mm)]
    if not accounts:
        story.append(Paragraph("No accounts to report.", styles["Normal"]))
    story.append(Paragraph("This is a system-generated statement and does not require a signature.", small))
    doc.build(story)
    buf.seek(0)
    audit("Statement generated", "transactions", c.customer_code, f"{start}..{end} accounts={len(accounts)}")
    activity(c.id, "Statement issued", c.customer_code, f"{start:%d %b} – {end:%d %b %Y}")
    db.session.commit()
    return send_file(buf, as_attachment=True, mimetype="application/pdf",
                     download_name=f"statement_{c.customer_code}_{end:%Y%m%d}.pdf")


@bp.route("/<int:cid>/reveal", methods=["POST"])
@login_required
@require_perm("customer_profile", "edit")
def reveal(cid):
    c = ensure_branch_access(db.session.get(Customer, cid))
    audit("Sensitive data revealed", "customer_profile", c.customer_code, "PAN / mobile unmasked")
    activity(c.id, "Sensitive data viewed", c.customer_code, f"by {current_user.name}")
    db.session.commit()
    # One-time reveal: shown on the next page render only, then masked again.
    session["reveal"] = c.id
    return redirect(url_for("customers.profile", cid=c.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("customer_profile", "create")
@once_per_form
def new():
    branches = Branch.query.all() if current_user.sees_all_branches else [current_user.branch]
    if request.method == "POST":
        f = request.form
        errors = []
        name = f.get("name", "").strip()
        pan = f.get("pan", "").strip().upper()
        mobile = f.get("mobile", "").strip()
        if len(name) < 3:
            errors.append("Name is required.")
        if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan):
            errors.append("PAN must be in the format ABCDE1234F.")
        if not re.fullmatch(r"[6-9]\d{9}", mobile):
            errors.append("Mobile must be a valid 10-digit number.")
        email = f.get("email", "").strip()
        if email and not EMAIL_RE.fullmatch(email):
            errors.append("Enter a valid email address or leave it blank.")
        dob = parse_date(f.get("dob"))
        if dob and not date(1900, 1, 1) <= dob <= local_today():
            errors.append("Enter a real date of birth (not in the future).")
        from ..crypto import search_hash
        if Customer.query.filter_by(pan_hash=search_hash(pan)).first():
            errors.append("A customer with this PAN already exists.")
        branch_id = f.get("branch_id", type=int)
        if branch_id not in [b.id for b in branches]:
            errors.append("Invalid branch.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("customers/new.html", branches=branches, f=f)
        c = Customer(customer_code=unique_number(Customer, "customer_code", "CIF", 6), name=name,
                     dob=dob, gender=f.get("gender"), email=email or None,
                     address=f.get("address"), city=f.get("city"), occupation=f.get("occupation"),
                     branch_id=branch_id, kyc_status="Pending", created_at=datetime.utcnow())
        c.pan = pan
        c.mobile = mobile
        db.session.add(c)
        db.session.flush()
        activity(c.id, "Customer onboarded", c.customer_code, f"by {current_user.name}")
        audit("Customer created", "customer_profile", c.customer_code)
        db.session.commit()
        flash(f"Customer {c.customer_code} created. Upload KYC documents to complete onboarding.", "success")
        return redirect(url_for("customers.profile", cid=c.id))
    return render_template("customers/new.html", branches=branches, f={})


@bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
@require_perm("customer_profile", "edit")
@once_per_form
def edit(cid):
    """Changes to a customer's contact or risk details are maker-checker requests: nothing
    changes until a checker and a manager approve. New values are stored encrypted in the request."""
    from ..services import CUSTOMER_EDITABLE, create_approval, pending_request
    c = ensure_branch_access(db.session.get(Customer, cid))
    pending = pending_request("customer_update", c.id)
    if request.method == "POST":
        if pending:
            flash(f"A change request ({pending.request_number}) is already waiting for approval.", "warning")
            return redirect(url_for("customers.profile", cid=c.id, tab="approvals"))
        f = request.form
        mobile = f.get("mobile", "").strip()
        email = f.get("email", "").strip()
        problems = []
        if mobile and not re.fullmatch(r"[6-9]\d{9}", mobile):
            problems.append("Mobile must be a valid 10-digit Indian mobile number.")
        if email and not EMAIL_RE.fullmatch(email):
            problems.append("Enter a valid email address or leave it blank.")
        if f.get("risk_category") not in ("Low", "Medium", "High"):
            problems.append("Choose a risk category.")
        if problems:
            for p in problems:
                flash(p, "danger")
            return redirect(url_for("customers.edit", cid=c.id))
        changes = {}
        for field_name in CUSTOMER_EDITABLE:
            new_val = f.get(field_name, "").strip()
            limit = {"mobile": 10, "email": 160, "address": 255, "city": 80, "occupation": 80, "risk_category": 10}[field_name]
            new_val = new_val[:limit]
            if field_name == "mobile" and not new_val:
                continue
            if new_val != (getattr(c, field_name) or ""):
                changes[field_name] = new_val
        if not changes:
            flash("Nothing was changed.", "info")
            return redirect(url_for("customers.profile", cid=c.id))
        req = create_approval("customer_update", "customer", c.id, c, None,
                              f"Update {', '.join(sorted(changes))} for {c.customer_code}", {"changes": changes})
        audit("Customer change requested", "customer_profile", c.customer_code, ", ".join(sorted(changes)))
        db.session.commit()
        flash(f"Change request {req.request_number} sent for maker-checker approval. The customer's details "
              f"change once it is approved.", "success")
        return redirect(url_for("customers.profile", cid=c.id, tab="approvals"))
    audit("Sensitive data revealed", "customer_profile", c.customer_code, "edit form")
    db.session.commit()
    return render_template("customers/edit.html", c=c, pending=pending)


@bp.route("/search")
@login_required
def global_search():
    """Global search across modules — every section respects the employee's permissions."""
    if not any(current_user.can(m) for m in ("customer_profile", "customer_accounts", "loans")):
        abort(403)
    q = request.args.get("q", "").strip()[:80]
    results = {"customers": [], "accounts": [], "loans": []}
    if q:
        if current_user.can("customer_profile"):
            results["customers"] = search_customers(q, branch_scope(Customer.query, Customer)).limit(15).all()
        matched_ids = search_customers(q, Customer.query).with_entities(Customer.id)
        if current_user.can("customer_accounts"):
            results["accounts"] = branch_scope(Account.query, Account).filter(
                Account.customer_id.in_(matched_ids)).limit(15).all()
        if current_user.can("loans"):
            results["loans"] = branch_scope(Loan.query, Loan).filter(
                Loan.customer_id.in_(matched_ids)).limit(15).all()
        audit("Global search", "search", details=f"term length {len(q)}")
        db.session.commit()
        # One unambiguous hit: go straight there instead of showing a list of one.
        exact_loan = [ln for ln in results["loans"] if ln.loan_number == q.upper()]
        if len(exact_loan) == 1:
            return redirect(url_for("loans.detail", lid=exact_loan[0].id))
        if len(results["customers"]) == 1:
            on_account = any(a.account_number == q for a in results["accounts"])
            return redirect(url_for("customers.profile", cid=results["customers"][0].id,
                                    **({"tab": "accounts"} if on_account else {})))
    return render_template("search/results.html", q=q, results=results)
