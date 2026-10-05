from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import TICKET_STATUSES, Customer, Employee, Ticket, TicketNote
from ..security import customer_from_request, activity, audit, branch_scope, ensure_branch_access, require_perm
from ..services import search_customers
from ..utils import unique_number

bp = Blueprint("tickets", __name__, url_prefix="/tickets")
CATEGORIES = ["Cards", "Charges", "Profile", "Services", "Digital", "Loans", "Deposits", "Other"]


@bp.route("/")
@login_required
@require_perm("tickets", "view")
def index():
    q = branch_scope(Ticket.query, Ticket)
    status = request.args.get("status", "")
    term = request.args.get("q", "").strip()
    counts = dict(q.with_entities(Ticket.status, func.count()).group_by(Ticket.status).all())
    if status:
        q = q.filter(Ticket.status == status)
    if request.args.get("mine"):
        q = q.filter(Ticket.assigned_to == current_user.id)
    if term:
        ids = search_customers(term, Customer.query).with_entities(Customer.id)
        q = q.filter(db.or_(Ticket.customer_id.in_(ids), Ticket.ticket_number == term.upper()))
    page = request.args.get("page", 1, type=int)
    tickets = q.order_by(Ticket.created_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("tickets/index.html", tickets=tickets, status=status, term=term,
                           counts=counts, statuses=TICKET_STATUSES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_perm("tickets", "create")
def new():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        subject = request.form.get("subject", "").strip()
        if len(subject) < 4:
            flash("Please enter a subject.", "danger")
            return redirect(request.url)
        t = Ticket(ticket_number=unique_number(Ticket, "ticket_number", "TKT", 6), customer_id=customer.id,
                   branch_id=customer.branch_id, subject=subject, description=request.form.get("description"),
                   category=request.form.get("category"), priority=request.form.get("priority", "Medium"),
                   status="Open", created_by=current_user.id)
        db.session.add(t)
        db.session.flush()
        db.session.add(TicketNote(ticket_id=t.id, employee_id=current_user.id, note="Ticket created.", status_change="Open"))
        activity(customer.id, "Ticket raised", t.ticket_number, subject)
        audit("Ticket created", "tickets", t.ticket_number)
        db.session.commit()
        flash(f"Ticket {t.ticket_number} created.", "success")
        return redirect(url_for("tickets.detail", tid=t.id))
    return render_template("tickets/new.html", customer=customer, categories=CATEGORIES)


@bp.route("/<int:tid>", methods=["GET", "POST"])
@login_required
@require_perm("tickets", "view")
def detail(tid):
    t = ensure_branch_access(db.session.get(Ticket, tid))
    if request.method == "POST":
        if not current_user.can("tickets", "edit"):
            abort(403)
        new_status = request.form.get("status")
        assignee = request.form.get("assigned_to", type=int)
        note = request.form.get("note", "").strip()
        changes = []
        if new_status in TICKET_STATUSES and new_status != t.status:
            changes.append(f"{t.status} → {new_status}")
            t.status = new_status
            if new_status in ("Resolved", "Closed"):
                t.resolved_at = datetime.utcnow()
        if assignee and assignee != t.assigned_to:
            emp = db.session.get(Employee, assignee)
            if emp and emp.branch_id == t.branch_id:
                t.assigned_to = emp.id
                changes.append(f"assigned to {emp.name}")
                if t.status == "Open":
                    t.status = "Assigned"
        if changes or note:
            t.updated_at = datetime.utcnow()
            db.session.add(TicketNote(ticket_id=t.id, employee_id=current_user.id, note=note,
                                      status_change="; ".join(changes) or None))
            audit("Ticket updated", "tickets", t.ticket_number, "; ".join(changes) or "note added")
            activity(t.customer_id, "Ticket updated", t.ticket_number, "; ".join(changes) or note[:80])
            db.session.commit()
            flash("Ticket updated.", "success")
        return redirect(url_for("tickets.detail", tid=t.id))
    staff = Employee.query.filter_by(branch_id=t.branch_id, is_active_flag=True).order_by(Employee.name).all()
    return render_template("tickets/detail.html", t=t, notes=t.notes.all(), staff=staff, statuses=TICKET_STATUSES)
