import io
from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .. import storage, workqueue
from ..extensions import db
from ..models import DOC_TYPES, Customer, Document, DocumentVerification, Loan, Setting
from ..security import once_per_form, customer_from_request, activity, audit, branch_scope, ensure_branch_access, require_perm
from ..utils import parse_date, local_today

bp = Blueprint("kyc", __name__, url_prefix="/kyc")



def _refresh_customer_kyc(customer):
    """KYC status from the *current* document of each type (superseded copies are ignored),
    so verifying a renewed passport clears an earlier expiry."""
    current = {}
    for d in customer.documents.filter(Document.category == "KYC", Document.status != "Superseded") \
            .order_by(Document.uploaded_at).all():
        prev = current.get(d.doc_type)
        # a verified renewal wins over an expired or rejected older copy
        if prev is None or d.status in ("Verified", "Pending") or prev.status not in ("Verified",):
            current[d.doc_type] = d
    docs = list(current.values())
    if any(d.status == "Expired" for d in docs):
        customer.kyc_status = "Expired"
    elif docs and all(d.status == "Verified" for d in docs) and {"PAN Card", "Aadhaar"} <= set(current):
        customer.kyc_status = "Verified"
    elif any(d.status == "Rejected" for d in docs):
        customer.kyc_status = "Rejected"
    else:
        customer.kyc_status = "Pending"


@bp.route("/")
@login_required
@require_perm("kyc", "view")
def index():
    view = request.args.get("view", "pending")
    today = local_today()
    warn = Setting.get_int("kyc_expiry_warning_days")
    q = Document.query.join(Customer)
    q = branch_scope(q, Customer)
    counts = {
        "pending": q.filter(Document.status == "Pending").count(),
        "expiring": q.filter(Document.expiry_date != None, Document.expiry_date >= today,  # noqa
                             Document.expiry_date <= today + timedelta(days=warn)).count(),
        "expired": q.filter(Document.status == "Expired").count(),
        "verified": q.filter(Document.status == "Verified").count(),
        "customers_pending": branch_scope(Customer.query, Customer).filter(Customer.kyc_status == "Pending").count(),
        "verified_month": q.filter(Document.status == "Verified",
                                   Document.verified_at >= datetime.combine(today.replace(day=1), datetime.min.time())).count(),
    }
    if view == "expiring":
        q = q.filter(Document.expiry_date != None, Document.expiry_date >= today,  # noqa
                     Document.expiry_date <= today + timedelta(days=warn)).order_by(Document.expiry_date)
    elif view == "expired":
        q = q.filter(Document.status == "Expired").order_by(Document.expiry_date.desc())
    elif view == "verified":
        q = q.filter(Document.status == "Verified").order_by(Document.verified_at.desc())
    elif view == "all":
        q = q.order_by(Document.uploaded_at.desc())
    else:
        view = "pending"
        q = q.filter(Document.status == "Pending").order_by(Document.uploaded_at)
    term = request.args.get("q", "").strip()
    if term:
        from ..services import search_customers
        q = q.filter(Document.customer_id.in_(search_customers(term, Customer.query).with_entities(Customer.id)))
    page = request.args.get("page", 1, type=int)
    docs = q.paginate(page=page, per_page=25, error_out=False)
    return render_template("kyc/index.html", docs=docs, view=view, counts=counts, term=term)


@bp.route("/upload", methods=["GET", "POST"])
@login_required
@require_perm("kyc", "create")
@once_per_form
def upload():
    customer = customer_from_request()
    if request.method == "POST" and customer:
        f = request.form
        file = request.files.get("file")
        doc_type = f.get("doc_type")
        if doc_type not in DOC_TYPES:
            flash("Select a document type.", "danger")
            return redirect(request.url)
        if not file or not file.filename:
            flash("Attach the scanned document (PDF, PNG or JPG). A document cannot be verified without it.", "danger")
            return redirect(request.url)
        original = secure_filename(file.filename)[:200] or "document"
        try:
            stored = storage.save(original, file.stream.read(storage.MAX_BYTES + 1))  # encrypted at rest
        except storage.StorageError as exc:
            flash(str(exc), "danger")
            return redirect(request.url)
        loan_id = f.get("loan_id", type=int)
        if loan_id:
            loan = db.session.get(Loan, loan_id)
            if not loan or loan.customer_id != customer.id:
                abort(400)
        doc = Document(customer_id=customer.id, loan_id=loan_id or None,
                       category="Loan" if loan_id else ("KYC" if doc_type in DOC_TYPES[:7] else "Other"),
                       doc_type=doc_type, file_name=original, stored_name=stored,
                       issue_date=parse_date(f.get("issue_date")), expiry_date=parse_date(f.get("expiry_date")),
                       status="Pending", uploaded_by=current_user.id, remarks=f.get("remarks"))
        doc.doc_number = f.get("doc_number", "").strip()
        db.session.add(doc)
        db.session.flush()
        db.session.add(DocumentVerification(document_id=doc.id, employee_id=current_user.id, action="Uploaded"))
        _refresh_customer_kyc(customer)
        activity(customer.id, "Document uploaded", doc.doc_type)
        audit("Document uploaded", "kyc", f"DOC-{doc.id}", f"{doc.doc_type} for {customer.customer_code}")
        db.session.commit()
        flash(f"{doc_type} uploaded and queued for verification.", "success")
        return redirect(url_for("customers.profile", cid=customer.id, tab="documents"))
    loans = customer.loans.all() if customer else []
    return render_template("kyc/upload.html", customer=customer, doc_types=DOC_TYPES, loans=loans)


@bp.route("/doc/<int:did>")
@login_required
@require_perm("kyc", "view")
def document(did):
    doc = db.session.get(Document, did) or abort(404)
    ensure_branch_access(doc.customer)
    audit("Document accessed", "kyc", f"DOC-{doc.id}", f"{doc.doc_type} of {doc.customer.customer_code}")
    db.session.commit()
    return render_template("kyc/document.html", doc=doc, history=doc.history.all())


@bp.route("/doc/<int:did>/file")
@login_required
@require_perm("kyc", "view")
def download(did):
    doc = db.session.get(Document, did) or abort(404)
    ensure_branch_access(doc.customer)
    if not doc.stored_name:
        abort(404)
    try:
        data = storage.load(doc.stored_name)
    except storage.StorageError as exc:
        audit("Document file unavailable", "kyc", f"DOC-{doc.id}", str(exc), result="failed")
        db.session.commit()
        abort(404)
    audit("Document file downloaded", "kyc", f"DOC-{doc.id}", doc.doc_type)
    db.session.commit()
    resp = send_file(io.BytesIO(data), mimetype=storage.mimetype_for(doc.file_name),
                     as_attachment=True, download_name=doc.file_name or "document")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/doc/<int:did>/verify", methods=["POST"])
@login_required
@once_per_form
def verify(did):
    doc = db.session.get(Document, did) or abort(404)
    ensure_branch_access(doc.customer)
    decision = request.form.get("decision")
    if decision not in ("approve", "reject") or not current_user.can("kyc", decision):
        abort(403)
    if doc.uploaded_by == current_user.id:
        flash("Maker-checker: you cannot verify a document you uploaded.", "danger")
        return redirect(url_for("kyc.document", did=did))
    if doc.status != "Pending":
        flash(f"This document is already {doc.status.lower()}. Nothing was changed.", "warning")
        return redirect(url_for("kyc.document", did=did))
    remarks = request.form.get("remarks", "").strip()[:250]
    if decision == "reject" and not remarks:
        flash("Enter the reason for rejecting, so the customer can be told what to bring.", "danger")
        return redirect(url_for("kyc.document", did=did))
    doc.status = "Verified" if decision == "approve" else "Rejected"
    if doc.status == "Verified":
        # Older copies of the same document are replaced by this verified one (renewal).
        Document.query.filter(Document.customer_id == doc.customer_id, Document.doc_type == doc.doc_type,
                              Document.id != doc.id, Document.category == doc.category,
                              Document.status.in_(["Verified", "Expired", "Rejected"])).update(
            {"status": "Superseded"}, synchronize_session=False)
    doc.verified_by = current_user.id
    doc.verified_at = datetime.utcnow()
    doc.remarks = remarks or doc.remarks
    db.session.add(DocumentVerification(document_id=doc.id, employee_id=current_user.id,
                                        action=doc.status, remarks=remarks))
    _refresh_customer_kyc(doc.customer)
    activity(doc.customer_id, "Document verified" if decision == "approve" else "Document rejected",
             doc.doc_type, remarks)
    if doc.category == "KYC":
        activity(doc.customer_id, "KYC updated", doc.customer.customer_code, f"KYC status: {doc.customer.kyc_status}")
    audit(f"Document {doc.status.lower()}", "kyc", f"DOC-{doc.id}", remarks)
    db.session.commit()
    flash(f"{doc.doc_type} for {doc.customer.name} marked {doc.status}. Customer KYC: {doc.customer.kyc_status}.", "success")
    nxt = workqueue.next_kyc(after_id=doc.id)  # never a redirect target taken from the request
    if nxt:
        flash(f"Next document to verify: {nxt.doc_type} for {nxt.customer.name}.", "info")
        return redirect(url_for("kyc.document", did=nxt.id))
    flash("No more documents waiting for your verification.", "info")
    return redirect(url_for("kyc.index"))
