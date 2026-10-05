import json
from datetime import date, datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .crypto import decrypt, encrypt, mask, search_hash
from .extensions import db

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

MODULES = [
    ("user_management", "User Management"),
    ("customer_accounts", "Customer Accounts"),
    ("customer_profile", "Customer Profile"),
    ("transactions", "Transactions"),
    ("accounting", "Accounting"),
    ("loans", "Loans"),
    ("loan_approval", "Loan Approval"),
    ("interest", "Interest"),
    ("deposits", "Deposits & FD"),
    ("kyc", "KYC & Documents"),
    ("tickets", "Customer Tickets"),
    ("reports", "Reports"),
    ("branch_management", "Branch Management"),
    ("audit_logs", "Audit Logs"),
    ("alerts", "Alerts & Notifications"),
    ("settings", "Settings"),
]
MODULE_LABELS = dict(MODULES)
ACTIONS = ["view", "create", "edit", "delete", "approve", "reject", "export"]

LOAN_TYPES = [
    ("gold", "Gold Loan"),
    ("personal", "Personal Loan"),
    ("home", "Home Loan"),
    ("vehicle", "Vehicle Loan"),
    ("business", "Business Loan"),
    ("education", "Education Loan"),
    ("other", "Other Loans"),
]
LOAN_TYPE_LABELS = dict(LOAN_TYPES)
LOAN_STATUSES = ["Pending", "Active", "Overdue", "Closed", "Rejected"]
TICKET_STATUSES = ["Open", "Assigned", "In Progress", "Pending", "Resolved", "Closed"]
DOC_TYPES = ["PAN Card", "Aadhaar", "Passport", "Voter ID", "Driving Licence",
             "Address Proof", "Photograph", "Income Proof", "Loan Agreement",
             "Gold Valuation Certificate", "Property Papers", "Vehicle RC", "Other"]


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# --------------------------------------------------------------------------
# Organisation, employees, RBAC
# --------------------------------------------------------------------------

class Branch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(80))
    target_deposits = db.Column(db.Float, default=0)
    target_loans = db.Column(db.Float, default=0)
    target_accounts = db.Column(db.Integer, default=0)

    def __str__(self):
        return f"{self.name} ({self.code})"


class Role(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), unique=True, nullable=False)
    description = db.Column(db.String(255))
    level = db.Column(db.Integer, default=10)  # 100 super admin, 80 manager ...
    is_system = db.Column(db.Boolean, default=False)
    permissions = db.relationship("Permission", backref="role", cascade="all, delete-orphan",
                                  lazy="selectin")

    def perm(self, module):
        for p in self.permissions:
            if p.module == module:
                return p
        return None


class Permission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False)
    module = db.Column(db.String(40), nullable=False)
    can_view = db.Column(db.Boolean, default=False)
    can_create = db.Column(db.Boolean, default=False)
    can_edit = db.Column(db.Boolean, default=False)
    can_delete = db.Column(db.Boolean, default=False)
    can_approve = db.Column(db.Boolean, default=False)
    can_reject = db.Column(db.Boolean, default=False)
    can_export = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint("role_id", "module"),)

    def allows(self, action):
        return bool(getattr(self, f"can_{action}", False))


class Employee(db.Model, UserMixin, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_code = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"))
    is_active_flag = db.Column(db.Boolean, default=True)
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_secret = db.Column(db.String(64))
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    last_login = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime, default=datetime.utcnow)
    must_change_password = db.Column(db.Boolean, default=False)
    session_token = db.Column(db.String(64))

    role = db.relationship("Role", lazy="joined")
    branch = db.relationship("Branch", lazy="joined")

    @property
    def is_active(self):
        return bool(self.is_active_flag)

    def get_id(self):
        # Session token is part of the id so that a password change / forced
        # logout invalidates every existing session for this employee.
        return f"{self.id}:{self.session_token or ''}"

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw, method="scrypt")
        self.password_changed_at = datetime.utcnow()

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_super_admin(self):
        return self.role is not None and self.role.level >= 100

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    def can(self, module, action="view"):
        if not self.role:
            return False
        if self.is_super_admin:
            return True
        p = self.role.perm(module)
        return bool(p and p.allows(action))

    @property
    def sees_all_branches(self):
        return self.is_super_admin or self.can("branch_management", "view") and self.role.level >= 90


class LoginHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    email = db.Column(db.String(160))
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(255))
    success = db.Column(db.Boolean, default=False)
    reason = db.Column(db.String(120))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    employee = db.relationship("Employee")


# --------------------------------------------------------------------------
# Customers & products
# --------------------------------------------------------------------------

class Customer(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    customer_code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    dob = db.Column(db.Date)
    gender = db.Column(db.String(10))
    email = db.Column(db.String(160))
    address = db.Column(db.String(255))
    city = db.Column(db.String(80))
    occupation = db.Column(db.String(80))
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    kyc_status = db.Column(db.String(20), default="Pending")  # Pending/Verified/Expired/Rejected
    risk_category = db.Column(db.String(10), default="Low")
    status = db.Column(db.String(20), default="Active")
    _pan = db.Column("pan_enc", db.String(255))
    pan_hash = db.Column(db.String(64), index=True)
    _mobile = db.Column("mobile_enc", db.String(255))
    mobile_hash = db.Column(db.String(64), index=True)

    branch = db.relationship("Branch")
    accounts = db.relationship("Account", backref="customer", lazy="dynamic")
    loans = db.relationship("Loan", backref="customer", lazy="dynamic")
    deposits = db.relationship("FixedDeposit", backref="customer", lazy="dynamic")

    @property
    def pan(self):
        return decrypt(self._pan)

    @pan.setter
    def pan(self, value):
        value = (value or "").strip().upper()
        self._pan = encrypt(value)
        self.pan_hash = search_hash(value)

    @property
    def mobile(self):
        return decrypt(self._mobile)

    @mobile.setter
    def mobile(self, value):
        value = (value or "").strip().replace(" ", "")
        self._mobile = encrypt(value)
        self.mobile_hash = search_hash(value)

    @property
    def primary_account_number(self):
        acc = self.accounts.filter(Account.status != "Closed").order_by(Account.opened_on).first()
        return acc.account_number if acc else None

    @property
    def pan_masked(self):
        return mask(self.pan)

    @property
    def mobile_masked(self):
        return mask(self.mobile)


class Account(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    account_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    account_type = db.Column(db.String(20), nullable=False)  # Savings / Current
    balance = db.Column(db.Float, default=0)
    interest_rate = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default="Active")  # Pending/Active/Inactive/Dormant/Closed
    opened_on = db.Column(db.Date, default=date.today)
    closed_on = db.Column(db.Date)
    last_txn_on = db.Column(db.Date)
    branch = db.relationship("Branch")


class FixedDeposit(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    fd_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    principal = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    maturity_date = db.Column(db.Date, nullable=False)
    maturity_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="Active")  # Pending/Active/Matured/Closed
    branch = db.relationship("Branch")


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    txn_ref = db.Column(db.String(24), unique=True, nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    txn_type = db.Column(db.String(10), nullable=False)  # Credit / Debit
    channel = db.Column(db.String(20))  # Cash, NEFT, IMPS, UPI, Cheque, Transfer
    category = db.Column(db.String(30))  # Deposit, Withdrawal, Loan EMI, Interest, Fee ...
    amount = db.Column(db.Float, nullable=False)
    balance_after = db.Column(db.Float)
    description = db.Column(db.String(255))
    flagged = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))

    account = db.relationship("Account")
    customer = db.relationship("Customer")


class Loan(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    loan_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"))
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    loan_type = db.Column(db.String(20), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    outstanding = db.Column(db.Float, default=0)
    interest_rate = db.Column(db.Float, nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    emi = db.Column(db.Float, default=0)
    disbursed_on = db.Column(db.Date)
    next_due_date = db.Column(db.Date)
    overdue_days = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="Pending", index=True)
    purpose = db.Column(db.String(255))
    gold_weight_grams = db.Column(db.Float)
    gold_purity = db.Column(db.String(10))
    gold_items = db.Column(db.String(255))
    gold_valuation = db.Column(db.Float)
    collateral = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))

    branch = db.relationship("Branch")
    account = db.relationship("Account")
    repayments = db.relationship("LoanRepayment", backref="loan", lazy="dynamic",
                                 order_by="LoanRepayment.paid_on.desc()")

    @property
    def type_label(self):
        return LOAN_TYPE_LABELS.get(self.loan_type, self.loan_type)

    @property
    def total_interest_paid(self):
        return sum(r.interest_component or 0 for r in self.repayments)

    @property
    def total_principal_paid(self):
        return sum(r.principal_component or 0 for r in self.repayments)


class LoanRepayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"), nullable=False)
    due_date = db.Column(db.Date)
    paid_on = db.Column(db.Date)
    amount = db.Column(db.Float, nullable=False)
    principal_component = db.Column(db.Float, default=0)
    interest_component = db.Column(db.Float, default=0)
    penalty = db.Column(db.Float, default=0)
    mode = db.Column(db.String(20))
    status = db.Column(db.String(20), default="Paid")  # Paid / Late


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"))
    category = db.Column(db.String(20), default="KYC")  # KYC / Loan / Other
    doc_type = db.Column(db.String(60), nullable=False)
    doc_number_enc = db.Column(db.String(255))
    file_name = db.Column(db.String(255))
    stored_name = db.Column(db.String(255))
    issue_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date, index=True)
    status = db.Column(db.String(20), default="Pending")  # Pending/Verified/Rejected/Expired
    remarks = db.Column(db.String(255))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    verified_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    verified_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", backref=db.backref("documents", lazy="dynamic"))
    loan = db.relationship("Loan", backref=db.backref("documents", lazy="dynamic"))
    uploader = db.relationship("Employee", foreign_keys=[uploaded_by])
    verifier = db.relationship("Employee", foreign_keys=[verified_by])

    @property
    def doc_number(self):
        return decrypt(self.doc_number_enc)

    @doc_number.setter
    def doc_number(self, value):
        self.doc_number_enc = encrypt(value)

    @property
    def doc_number_masked(self):
        return mask(self.doc_number)

    @property
    def days_to_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - date.today()).days


class DocumentVerification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("document.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    action = db.Column(db.String(30))
    remarks = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    document = db.relationship("Document", backref=db.backref("history", lazy="dynamic",
                               order_by="DocumentVerification.timestamp.desc()"))
    employee = db.relationship("Employee")


# --------------------------------------------------------------------------
# Tickets, approvals, timeline, audit, alerts
# --------------------------------------------------------------------------

class Ticket(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(40))
    priority = db.Column(db.String(10), default="Medium")
    status = db.Column(db.String(20), default="Open", index=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("employee.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", backref=db.backref("tickets", lazy="dynamic"))
    assignee = db.relationship("Employee", foreign_keys=[assigned_to])
    creator = db.relationship("Employee", foreign_keys=[created_by])


class TicketNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    note = db.Column(db.Text)
    status_change = db.Column(db.String(60))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ticket = db.relationship("Ticket", backref=db.backref("notes", lazy="dynamic",
                             order_by="TicketNote.timestamp.desc()"))
    employee = db.relationship("Employee")


class ApprovalRequest(db.Model, TimestampMixin):
    """Maker-checker: maker creates -> checker reviews -> manager approves/rejects."""
    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(20), unique=True, nullable=False)
    request_type = db.Column(db.String(40), nullable=False)  # loan_sanction, account_open ...
    module = db.Column(db.String(40), nullable=False)
    ref_type = db.Column(db.String(30))
    ref_id = db.Column(db.Integer)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"))
    amount = db.Column(db.Float)
    summary = db.Column(db.String(255))
    payload_json = db.Column(db.Text)
    status = db.Column(db.String(20), default="Pending Review", index=True)
    maker_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    checker_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    checker_remarks = db.Column(db.String(255))
    checked_at = db.Column(db.DateTime)
    approver_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    approver_remarks = db.Column(db.String(255))
    decided_at = db.Column(db.DateTime)

    customer = db.relationship("Customer")
    branch = db.relationship("Branch")
    maker = db.relationship("Employee", foreign_keys=[maker_id])
    checker = db.relationship("Employee", foreign_keys=[checker_id])
    approver = db.relationship("Employee", foreign_keys=[approver_id])

    TYPE_LABELS = {
        "loan_sanction": "Loan Sanction",
        "account_open": "Account Opening",
        "account_close": "Account Closure",
        "fd_create": "Fixed Deposit Booking",
    }

    @property
    def type_label(self):
        return self.TYPE_LABELS.get(self.request_type, self.request_type)

    @property
    def payload(self):
        return json.loads(self.payload_json or "{}")


class Activity(db.Model):
    """Customer activity timeline."""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    action = db.Column(db.String(80), nullable=False)
    reference = db.Column(db.String(60))
    details = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    employee = db.relationship("Employee")
    customer = db.relationship("Customer")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"))
    email = db.Column(db.String(160))
    action = db.Column(db.String(80), nullable=False, index=True)
    module = db.Column(db.String(40), index=True)
    reference = db.Column(db.String(80))
    details = db.Column(db.String(500))
    ip_address = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    employee = db.relationship("Employee")


class Alert(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(30), nullable=False, index=True)
    severity = db.Column(db.String(10), default="Medium")  # Low/Medium/High/Critical
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.String(500))
    module = db.Column(db.String(40))  # used to route to employees with view rights
    reference = db.Column(db.String(80))
    link = db.Column(db.String(200))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"))
    is_read = db.Column(db.Boolean, default=False)
    is_resolved = db.Column(db.Boolean, default=False)
    resolved_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    resolved_at = db.Column(db.DateTime)
    customer = db.relationship("Customer")

    TYPE_LABELS = {
        "emi_due": "EMI Due", "overdue_loan": "Overdue Loan", "kyc_expiry": "KYC Expiry",
        "pending_approval": "Pending Approval", "document_expiry": "Document Expiry",
        "unusual_txn": "Unusual Transaction", "failed_login": "Failed Login",
        "system": "System Alert", "fraud_risk": "Fraud / Risk",
    }

    @property
    def type_label(self):
        return self.TYPE_LABELS.get(self.alert_type, self.alert_type)


class AlertRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    rule_type = db.Column(db.String(30), nullable=False)
    # high_value_txn | txn_velocity | failed_logins | dormant_activity
    threshold = db.Column(db.Float, default=0)
    window_minutes = db.Column(db.Integer, default=60)
    severity = db.Column(db.String(10), default="High")
    route_module = db.Column(db.String(40), default="alerts")
    enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(255))


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(255))

    DEFAULTS = {
        "session_timeout_minutes": "15",
        "max_failed_logins": "5",
        "lockout_minutes": "15",
        "password_expiry_days": "90",
        "enforce_mfa": "0",
        "kyc_expiry_warning_days": "30",
        "emi_due_warning_days": "5",
        "bank_name": "Local Bank",
    }

    @classmethod
    def get(cls, key, default=None):
        row = db.session.get(cls, key)
        if row is not None:
            return row.value
        return cls.DEFAULTS.get(key, default)

    @classmethod
    def get_int(cls, key):
        try:
            return int(cls.get(key))
        except (TypeError, ValueError):
            return int(cls.DEFAULTS.get(key, 0))

    @classmethod
    def set(cls, key, value):
        row = db.session.get(cls, key)
        if row is None:
            row = cls(key=key)
            db.session.add(row)
        row.value = str(value)
