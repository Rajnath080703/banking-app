import json
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .crypto import decrypt, encrypt, mask, search_hash
from .extensions import db
from .utils import local_today

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
ACTIONS = ["view", "create", "edit", "delete", "approve", "reject", "export", "print"]

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


# Money is stored as exact NUMERIC and handled in Python as decimal.Decimal (see money.py).
Money = db.Numeric(18, 2)
Rate = db.Numeric(7, 4)
Grams = db.Numeric(10, 3)
ACCOUNT_STATUSES = ("Pending", "Active", "Inactive", "Dormant", "Frozen", "Closed", "Rejected")


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
    target_deposits = db.Column(Money, default=0)
    target_loans = db.Column(Money, default=0)
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
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False, index=True)
    module = db.Column(db.String(40), nullable=False)
    can_view = db.Column(db.Boolean, default=False)
    can_create = db.Column(db.Boolean, default=False)
    can_edit = db.Column(db.Boolean, default=False)
    can_delete = db.Column(db.Boolean, default=False)
    can_approve = db.Column(db.Boolean, default=False)
    can_reject = db.Column(db.Boolean, default=False)
    can_export = db.Column(db.Boolean, default=False)
    can_print = db.Column(db.Boolean, default=False)
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
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), index=True)
    is_active_flag = db.Column(db.Boolean, default=True)
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_secret = db.Column(db.String(255))  # Fernet-encrypted
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    last_login = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime, default=datetime.utcnow)
    must_change_password = db.Column(db.Boolean, default=False)
    session_token = db.Column(db.String(64))
    last_login_ip = db.Column(db.String(64))
    mfa_last_step = db.Column(db.BigInteger)  # last accepted TOTP time-step (blocks code replay)

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
        return self.is_super_admin or (self.can("branch_management", "view") and self.role.level >= 90)


class LoginHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
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
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
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
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    account_type = db.Column(db.String(20), nullable=False)  # Savings / Current
    balance = db.Column(Money, default=0)
    interest_rate = db.Column(Rate, default=0)
    status = db.Column(db.String(20), default="Active")  # see ACCOUNT_STATUSES; Frozen = no debits
    opened_on = db.Column(db.Date, default=local_today)
    closed_on = db.Column(db.Date)
    last_txn_on = db.Column(db.Date)
    branch = db.relationship("Branch")
    __table_args__ = (
        db.CheckConstraint("balance >= 0", name="ck_account_balance_non_negative"),
        db.CheckConstraint(_in("status", ACCOUNT_STATUSES), name="ck_account_status"),
        db.CheckConstraint(_in("account_type", ("Savings", "Current")), name="ck_account_type"),
    )


class FixedDeposit(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    fd_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), index=True)  # funding & maturity payout account
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    principal = db.Column(Money, nullable=False)
    interest_rate = db.Column(Rate, nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    maturity_date = db.Column(db.Date, nullable=False)
    maturity_amount = db.Column(Money, nullable=False)
    status = db.Column(db.String(20), default="Active")  # Pending/Active/Matured/Closed/Rejected
    paid_out_on = db.Column(db.Date)
    paid_out_amount = db.Column(Money)
    premature = db.Column(db.Boolean, default=False)
    branch = db.relationship("Branch")
    account = db.relationship("Account")
    __table_args__ = (
        db.CheckConstraint("principal > 0", name="ck_fd_principal_positive"),
        db.CheckConstraint("maturity_amount >= principal", name="ck_fd_maturity_ge_principal"),
        db.CheckConstraint("tenure_months > 0", name="ck_fd_tenure_positive"),
        db.CheckConstraint(_in("status", ("Pending", "Active", "Matured", "Closed", "Rejected")), name="ck_fd_status"),
    )


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    txn_ref = db.Column(db.String(24), unique=True, nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    txn_type = db.Column(db.String(10), nullable=False)  # Credit / Debit
    channel = db.Column(db.String(20))  # Cash, NEFT, IMPS, UPI, Cheque, Transfer
    category = db.Column(db.String(30))  # Deposit, Withdrawal, Loan EMI, Interest, Fee ...
    amount = db.Column(Money, nullable=False)
    balance_after = db.Column(Money)
    description = db.Column(db.String(255))
    flagged = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(12), default="Posted", nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entry.id"), index=True)

    account = db.relationship("Account")
    customer = db.relationship("Customer")
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_txn_amount_positive"),
        db.CheckConstraint(_in("txn_type", ("Credit", "Debit")), name="ck_txn_type"),
        db.Index("ix_txn_branch_time", "branch_id", "timestamp"),
        db.Index("ix_txn_account_time", "account_id", "timestamp"),
    )


class Loan(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    loan_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    loan_type = db.Column(db.String(20), nullable=False, index=True)
    amount = db.Column(Money, nullable=False)  # sanctioned amount
    disbursed_amount = db.Column(Money, default=0)
    outstanding = db.Column(Money, default=0)  # outstanding principal
    interest_rate = db.Column(Rate, nullable=False)
    tenure_months = db.Column(db.Integer, nullable=False)
    emi = db.Column(Money, default=0)
    disbursed_on = db.Column(db.Date)
    next_due_date = db.Column(db.Date)
    overdue_days = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="Pending", index=True)
    purpose = db.Column(db.String(255))
    gold_weight_grams = db.Column(Grams)
    gold_purity = db.Column(db.String(10))
    gold_items = db.Column(db.String(255))
    gold_valuation = db.Column(Money)
    collateral = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))

    branch = db.relationship("Branch")
    account = db.relationship("Account")
    repayments = db.relationship("LoanRepayment", backref="loan", lazy="dynamic",
                                 order_by="LoanRepayment.id.desc()")
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_loan_amount_positive"),
        db.CheckConstraint("outstanding >= 0 AND outstanding <= amount", name="ck_loan_outstanding_range"),
        db.CheckConstraint("interest_rate > 0 AND interest_rate <= 36", name="ck_loan_rate_range"),
        db.CheckConstraint("tenure_months BETWEEN 1 AND 360", name="ck_loan_tenure_range"),
        db.CheckConstraint(_in("status", ("Pending", "Active", "Overdue", "Closed", "Rejected")), name="ck_loan_status"),
        db.Index("ix_loan_branch_status", "branch_id", "status"),
    )

    @property
    def type_label(self):
        return LOAN_TYPE_LABELS.get(self.loan_type, self.loan_type)

    @property
    def total_interest_paid(self):
        return sum(r.interest_component or 0 for r in self.repayments)

    @property
    def total_principal_paid(self):
        return sum(r.principal_component or 0 for r in self.repayments)

    @property
    def total_penalty_paid(self):
        return sum(r.penalty or 0 for r in self.repayments)


class LoanRepayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"), nullable=False, index=True)
    due_date = db.Column(db.Date)
    paid_on = db.Column(db.Date)
    amount = db.Column(Money, nullable=False)
    principal_component = db.Column(Money, default=0)
    interest_component = db.Column(Money, default=0)
    penalty = db.Column(Money, default=0)
    mode = db.Column(db.String(20))
    status = db.Column(db.String(20), default="Paid")  # Paid / Late
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_repay_amount_positive"),
        db.CheckConstraint("principal_component >= 0 AND interest_component >= 0 AND penalty >= 0",
                           name="ck_repay_parts_non_negative"),
        db.CheckConstraint("principal_component + interest_component + penalty = amount",
                           name="ck_repay_parts_sum"),
    )


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"), index=True)
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
        return (self.expiry_date - local_today()).days


class DocumentVerification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("document.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
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
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
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
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
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
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), index=True)
    amount = db.Column(Money)
    summary = db.Column(db.String(255))
    payload_json = db.Column(db.Text)
    status = db.Column(db.String(20), default="Pending Review", index=True)
    maker_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
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
        "fd_close": "FD Premature Closure",
        "large_txn": "High-value Transaction",
        "txn_reversal": "Transaction Reversal",
        "fund_transfer": "Fund Transfer",
        "customer_update": "Customer Details Change",
        "loan_rate_change": "Loan Interest-rate Change",
        "account_freeze": "Account Freeze",
        "account_unfreeze": "Account Unfreeze",
        "role_permissions": "Role Permission Change",
        "interest_posting": "Savings Interest Posting",
    }

    @property
    def type_label(self):
        return self.TYPE_LABELS.get(self.request_type, self.request_type)

    @property
    def payload(self):
        """Request details. Stored encrypted because some requests carry customer PII."""
        raw = self.payload_json or "{}"
        if raw.startswith("enc:"):
            raw = decrypt(raw[4:])
        return json.loads(raw or "{}")

    @payload.setter
    def payload(self, value):
        self.payload_json = "enc:" + encrypt(json.dumps(value or {}))


class Activity(db.Model):
    """Customer activity timeline."""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
    action = db.Column(db.String(80), nullable=False)
    reference = db.Column(db.String(60))
    details = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    employee = db.relationship("Employee")
    customer = db.relationship("Customer")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
    email = db.Column(db.String(160))
    action = db.Column(db.String(80), nullable=False, index=True)
    module = db.Column(db.String(40), index=True)
    reference = db.Column(db.String(80))
    details = db.Column(db.String(500))
    ip_address = db.Column(db.String(64))
    result = db.Column(db.String(12), default="success")  # success / denied / failed
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    # Tamper evidence: rows are sealed into a SHA-256 hash chain (see audit_chain.py) and the
    # database refuses UPDATE / DELETE on sealed rows.
    prev_hash = db.Column(db.String(64))
    row_hash = db.Column(db.String(64), index=True)
    employee = db.relationship("Employee")

    def canonical(self):
        ts = self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f") if self.timestamp else ""
        return "|".join(str(x if x is not None else "") for x in (
            self.id, ts, self.employee_id, self.email, self.action, self.module,
            self.reference, self.details, self.ip_address, self.result))


class Alert(db.Model, TimestampMixin):
    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(30), nullable=False, index=True)
    severity = db.Column(db.String(10), default="Medium")  # Low/Medium/High/Critical
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.String(500))
    module = db.Column(db.String(40))  # used to route to employees with view rights
    reference = db.Column(db.String(80))
    link = db.Column(db.String(200))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), index=True)
    is_read = db.Column(db.Boolean, default=False)
    is_resolved = db.Column(db.Boolean, default=False)
    resolved_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    resolved_at = db.Column(db.DateTime)
    customer = db.relationship("Customer")
    __table_args__ = (
        # At most one open alert per (type, reference): duplicates are impossible even under concurrency.
        db.Index("uq_alert_open_ref", "alert_type", "reference", unique=True,
                 postgresql_where=db.text("is_resolved = false AND reference IS NOT NULL")),
    )

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
    threshold = db.Column(Money, default=0)
    window_minutes = db.Column(db.Integer, default=60)
    severity = db.Column(db.String(10), default="High")
    route_module = db.Column(db.String(40), default="alerts")
    enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(255))


# --------------------------------------------------------------------------
# Double-entry general ledger
# --------------------------------------------------------------------------

GL_ACCOUNTS = [
    # code, name, type (normal balance follows the type)
    ("1000", "Cash in hand", "Asset"),
    ("1100", "Clearing / settlement (NEFT, IMPS, UPI, cheque)", "Asset"),
    ("1200", "Loans and advances", "Asset"),
    ("2000", "Customer deposits — savings & current", "Liability"),
    ("2100", "Term deposits (FD)", "Liability"),
    ("4000", "Interest income on loans", "Income"),
    ("4100", "Penal charges income", "Income"),
    ("5000", "Interest expense on deposits", "Expense"),
]


class GLAccount(db.Model):
    code = db.Column(db.String(10), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    gl_type = db.Column(db.String(12), nullable=False)
    __table_args__ = (db.CheckConstraint(_in("gl_type", ("Asset", "Liability", "Income", "Expense", "Equity")),
                                         name="ck_gl_type"),)

    @property
    def debit_normal(self):
        return self.gl_type in ("Asset", "Expense")


class JournalEntry(db.Model):
    """One business event. Its lines must balance (enforced by a deferred database trigger)."""
    id = db.Column(db.Integer, primary_key=True)
    entry_ref = db.Column(db.String(24), unique=True, nullable=False)
    posted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    value_date = db.Column(db.Date, default=local_today, nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    narration = db.Column(db.String(255), nullable=False)
    source_type = db.Column(db.String(30))   # transaction / repayment / disbursal / fd / interest
    source_ref = db.Column(db.String(40), index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    lines = db.relationship("JournalLine", backref="entry", lazy="selectin", cascade="all, delete-orphan")


class JournalLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entry.id"), nullable=False, index=True)
    gl_code = db.Column(db.String(10), db.ForeignKey("gl_account.code"), nullable=False, index=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), index=True)  # customer sub-ledger
    loan_id = db.Column(db.Integer, db.ForeignKey("loan.id"), index=True)
    debit = db.Column(Money, nullable=False, default=0)
    credit = db.Column(Money, nullable=False, default=0)
    __table_args__ = (
        db.CheckConstraint("debit >= 0 AND credit >= 0 AND (debit = 0) <> (credit = 0)", name="ck_line_one_side"),
    )


class InterestRun(db.Model):
    """Register of savings-interest postings. The unique constraint makes it impossible to
    post interest twice for the same branch and period."""
    id = db.Column(db.Integer, primary_key=True)
    branch_id = db.Column(db.Integer, db.ForeignKey("branch.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    reference = db.Column(db.String(20))
    total = db.Column(Money, nullable=False, default=0)
    accounts = db.Column(db.Integer, nullable=False, default=0)
    posted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint("branch_id", "period_start", "period_end", name="uq_interest_run_period"),
                      db.CheckConstraint("period_end >= period_start", name="ck_interest_run_period"))


class InviteToken(db.Model):
    """One-time link to set a password (new employee invite or admin-initiated reset).
    Only a SHA-256 of the token is stored."""
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False, index=True)
    purpose = db.Column(db.String(10), nullable=False)  # invite / reset
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey("employee.id"))
    employee = db.relationship("Employee", foreign_keys=[employee_id])


class FormToken(db.Model):
    """One-time token per rendered form. Inserting it twice violates the primary key, so a
    double-click or a replayed POST can never execute a money operation twice."""
    token = db.Column(db.String(64), primary_key=True)
    used_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(255))

    DEFAULTS = {
        "session_timeout_minutes": "15",
        "max_failed_logins": "5",
        "lockout_minutes": "15",
        "password_expiry_days": "90",  # nosec B105 - a number of days, not a password
        "enforce_mfa": "0",
        "kyc_expiry_warning_days": "30",
        "emi_due_warning_days": "5",
        "bank_name": "Local Bank",
        "large_txn_approval_limit": "1000000",
        "penal_rate_pa": "24",            # penal charge, % a year, on an overdue instalment
        "fd_premature_penalty_pct": "1",  # rate reduction on premature FD closure
        "max_export_rows": "50000",
        "absolute_session_hours": "12",
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


# --------------------------------------------------------------------------
# PostgreSQL enforces VARCHAR lengths (SQLite never did). Trim over-long text
# from forms before it is written, instead of failing the whole request.
# --------------------------------------------------------------------------

from sqlalchemy import event as _sa_event  # noqa: E402
from sqlalchemy.orm import Session as _SASession  # noqa: E402


@_sa_event.listens_for(_SASession, "before_flush")
def _trim_strings(session, flush_context, instances):
    for obj in list(session.new) + list(session.dirty):
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        for col in table.columns:
            length = getattr(col.type, "length", None)
            if not length or not isinstance(col.type, db.String):
                continue
            attr = obj.__mapper__.get_property_by_column(col).key
            value = getattr(obj, attr, None)
            if isinstance(value, str) and len(value) > length:
                setattr(obj, attr, value[:length])
