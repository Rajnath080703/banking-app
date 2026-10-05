"""Initial setup: roles, permissions, the Super Admin account and demo data."""
import random
import secrets
from datetime import date, datetime, timedelta

import click

from .extensions import db
from .models import (ACTIONS, MODULES, Account, Activity, AlertRule, ApprovalRequest, AuditLog,
                     Branch, Customer, Document, Employee, FixedDeposit, Loan, LoanRepayment,
                     Permission, Role, Ticket, TicketNote, Transaction)
from .utils import add_months, calc_emi, fd_maturity

SUPER_ADMIN_EMAIL = "rajnath080703@gmail.com"
SUPER_ADMIN_PASSWORD = "Bank@3006"
DEMO_PASSWORD = "Demo@12345"

ALL = set(ACTIONS)
V = {"view"}
VC = {"view", "create"}
VCE = {"view", "create", "edit"}
VCEAR = {"view", "create", "edit", "approve", "reject"}

ROLE_MATRIX = {
    "Super Admin": (100, "Full access to every module and branch.", {m: ALL for m, _ in MODULES}),
    "Branch Manager": (80, "Runs a branch; final approver in maker-checker.", {
        "user_management": V, "customer_accounts": VCEAR | {"export"},
        "customer_profile": VCE | {"export"}, "transactions": VC | {"export"},
        "accounting": V | {"export"}, "loans": VCE | {"export"},
        "loan_approval": {"view", "approve", "reject"}, "interest": VCE,
        "deposits": VCEAR | {"export"}, "kyc": VCEAR | {"export"},
        "tickets": VCE | {"delete", "export"}, "reports": V | {"export"},
        "branch_management": VCE, "audit_logs": V | {"export"}, "alerts": VCE, "settings": V,
    }),
    "Senior Officer": (60, "Checker for loans, accounts, deposits and KYC.", {
        "customer_accounts": VCEAR, "customer_profile": VCE, "transactions": VC,
        "accounting": V, "loans": VCE, "loan_approval": {"view", "approve", "reject"},
        "interest": V, "deposits": VCEAR, "kyc": VCEAR, "tickets": VCE,
        "reports": V | {"export"}, "branch_management": V, "alerts": VCE,
    }),
    "Officer": (40, "Maker for accounts, loans, deposits; handles customers.", {
        "customer_accounts": VC, "customer_profile": V, "transactions": VC, "loans": VC,
        "interest": V, "deposits": VC, "kyc": VC, "tickets": VCE, "reports": V, "alerts": V,
    }),
    "Clerk": (20, "Front desk: lookups, cash transactions, document intake.", {
        "customer_accounts": V, "transactions": VC, "kyc": VC, "tickets": VC, "alerts": V,
    }),
}


def apply_matrix(role, matrix):
    existing = {p.module: p for p in role.permissions}
    for module, _ in MODULES:
        p = existing.get(module) or Permission(module=module)
        acts = matrix.get(module, set())
        for a in ACTIONS:
            setattr(p, f"can_{a}", a in acts)
        if module not in existing:
            role.permissions.append(p)


def ensure_core():
    """Idempotent: roles, rules, branches and the super admin."""
    for name, (level, desc, matrix) in ROLE_MATRIX.items():
        role = Role.query.filter_by(name=name).first()
        if not role:
            role = Role(name=name, level=level, description=desc, is_system=True)
            db.session.add(role)
            apply_matrix(role, matrix)
    if not Role.query.filter_by(name="Custom Role").first():
        custom = Role(name="Custom Role", level=30, is_system=False,
                      description="Template role — tailor permissions as needed.")
        db.session.add(custom)
        apply_matrix(custom, {"customer_accounts": V, "customer_profile": V})

    if not Branch.query.first():
        db.session.add_all([
            Branch(code="MAIN001", name="Main Branch", city="Chennai",
                   target_deposits=250_000_000, target_loans=180_000_000, target_accounts=120),
            Branch(code="ANNA002", name="Anna Nagar Branch", city="Chennai",
                   target_deposits=150_000_000, target_loans=110_000_000, target_accounts=80),
            Branch(code="CBE003", name="Coimbatore Branch", city="Coimbatore",
                   target_deposits=120_000_000, target_loans=90_000_000, target_accounts=60),
        ])

    if not AlertRule.query.first():
        db.session.add_all([
            AlertRule(name="High-value transaction", rule_type="high_value_txn",
                      threshold=500_000, severity="High", route_module="transactions",
                      description="Any single transaction at or above the threshold amount."),
            AlertRule(name="Transaction velocity", rule_type="txn_velocity", threshold=5,
                      window_minutes=30, severity="Medium", route_module="transactions",
                      description="Too many transactions on one account in a short window."),
            AlertRule(name="Multiple failed logins", rule_type="failed_logins", threshold=3,
                      severity="High", route_module="user_management",
                      description="Consecutive failed sign-in attempts for an employee."),
            AlertRule(name="Dormant account activity", rule_type="dormant_activity",
                      threshold=0, severity="High", route_module="customer_accounts",
                      description="Any transaction on an account marked Dormant."),
        ])
    db.session.flush()

    admin = Employee.query.filter_by(email=SUPER_ADMIN_EMAIL).first()
    if not admin:
        admin = Employee(employee_code="EMP0001", name="Rajnath", email=SUPER_ADMIN_EMAIL,
                         role=Role.query.filter_by(name="Super Admin").one(),
                         branch=Branch.query.filter_by(code="MAIN001").one(),
                         session_token=secrets.token_hex(16))
        admin.set_password(SUPER_ADMIN_PASSWORD)
        db.session.add(admin)
    db.session.commit()
    return admin


FIRST = ["Arun", "Priya", "Karthik", "Lakshmi", "Suresh", "Divya", "Ramesh", "Meena", "Vijay",
         "Anitha", "Senthil", "Kavya", "Prakash", "Deepa", "Ganesh", "Revathi", "Mohan", "Sangeetha",
         "Bala", "Nithya", "Harish", "Swathi", "Ashok", "Janani", "Dinesh", "Keerthi", "Manoj",
         "Pooja", "Rajesh", "Sowmya", "Venkat", "Uma", "Saravanan", "Geetha", "Murali", "Indhu"]
LAST = ["Kumar", "Raman", "Subramanian", "Krishnan", "Natarajan", "Iyer", "Pillai", "Murugan",
        "Sundaram", "Rajan", "Srinivasan", "Balaji", "Chandran", "Venkatesan", "Shankar"]
OCCUPATIONS = ["Salaried", "Business Owner", "Farmer", "Teacher", "Engineer", "Doctor",
               "Retired", "Student", "Self-employed", "Shopkeeper"]
TICKET_SUBJECTS = [("Debit card not working", "Cards"), ("Wrong charge on statement", "Charges"),
                   ("Update mobile number", "Profile"), ("Cheque book request", "Services"),
                   ("Net banking locked", "Digital"), ("Loan statement required", "Loans"),
                   ("EMI auto-debit failed", "Loans"), ("Interest certificate", "Deposits"),
                   ("UPI transaction pending", "Digital"), ("Address change request", "Profile")]


def seed_demo(n_customers=90):
    if Customer.query.first():
        return False
    rnd = random.Random(42)
    today = date.today()
    now = datetime.utcnow()
    branches = Branch.query.all()
    roles = {r.name: r for r in Role.query.all()}
    admin = Employee.query.filter_by(email=SUPER_ADMIN_EMAIL).one()

    staff = [admin]
    staff_specs = [("Branch Manager", "Meenakshi Sundaram", "manager"),
                   ("Senior Officer", "Karthik Raman", "senior"),
                   ("Officer", "Divya Natarajan", "officer"),
                   ("Clerk", "Suresh Babu", "clerk")]
    code = 2
    for b in branches:
        for role_name, name, handle in staff_specs:
            first = name.split()[0] if b.code == "MAIN001" else rnd.choice(FIRST)
            nm = name if b.code == "MAIN001" else f"{first} {rnd.choice(LAST)}"
            e = Employee(employee_code=f"EMP{code:04d}", name=nm,
                         email=f"{handle}.{b.code.lower()}@localbank.in", role=roles[role_name],
                         branch=b, session_token=secrets.token_hex(16))
            e.set_password(DEMO_PASSWORD)
            db.session.add(e)
            staff.append(e)
            code += 1
    db.session.flush()

    def staff_of(branch, role_name):
        return next(s for s in staff if s.branch_id == branch.id and s.role.name == role_name)

    def acct_no():
        return "50" + "".join(str(rnd.randint(0, 9)) for _ in range(10))

    customers = []
    for i in range(n_customers):
        b = branches[i % len(branches)]
        name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        created = now - timedelta(days=rnd.randint(1, 900))
        c = Customer(customer_code=f"CIF{100001 + i}", name=name,
                     dob=date(rnd.randint(1955, 2003), rnd.randint(1, 12), rnd.randint(1, 28)),
                     gender=rnd.choice(["Male", "Female"]),
                     email=f"{name.split()[0].lower()}{i}@example.com",
                     address=f"{rnd.randint(1, 200)}, {rnd.choice(['Gandhi St', 'Nehru Rd', 'Main Rd', 'Temple St', 'Lake View'])}",
                     city=b.city, occupation=rnd.choice(OCCUPATIONS), branch=b,
                     risk_category=rnd.choices(["Low", "Medium", "High"], [70, 22, 8])[0],
                     created_at=created)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        c.pan = ("".join(rnd.choices(letters, k=3)) + "P" + name.split()[-1][0] +
                 f"{rnd.randint(1000, 9999)}" + rnd.choice(letters))
        c.mobile = f"9{rnd.randint(100000000, 999999999)}"
        db.session.add(c)
        customers.append(c)
    db.session.flush()

    accounts = []
    for c in customers:
        for j in range(rnd.choice([1, 1, 2])):
            acc_type = "Savings" if j == 0 else "Current"
            opened = c.created_at.date() + timedelta(days=rnd.randint(0, 20))
            status = rnd.choices(["Active", "Inactive", "Dormant", "Closed"], [80, 8, 7, 5])[0]
            last_txn = {"Active": today - timedelta(days=rnd.randint(0, 30)),
                        "Inactive": today - timedelta(days=rnd.randint(190, 300)),
                        "Dormant": today - timedelta(days=rnd.randint(380, 700)),
                        "Closed": today - timedelta(days=rnd.randint(10, 200))}[status]
            a = Account(account_number=acct_no(), customer=c, branch_id=c.branch_id,
                        account_type=acc_type, interest_rate=3.0 if acc_type == "Savings" else 0,
                        balance=0 if status == "Closed" else round(rnd.uniform(2_000, 900_000), 2),
                        status=status, opened_on=min(opened, today), last_txn_on=last_txn,
                        closed_on=last_txn if status == "Closed" else None,
                        created_at=datetime.combine(min(opened, today), datetime.min.time()))
            db.session.add(a)
            accounts.append(a)
    db.session.flush()

    # Transactions over the past ~13 months, denser recently.
    channels = ["Cash", "NEFT", "IMPS", "UPI", "Cheque", "Transfer"]
    txn_n = 0
    active_accounts = [a for a in accounts if a.status == "Active"]
    for day_offset in range(395, -1, -1):
        d = today - timedelta(days=day_offset)
        per_day = rnd.randint(4, 9) if day_offset > 60 else rnd.randint(10, 22)
        for _ in range(per_day):
            a = rnd.choice(active_accounts)
            credit = rnd.random() < 0.55
            amount = round(rnd.choice([rnd.uniform(500, 25_000)] * 6 + [rnd.uniform(25_000, 200_000)]), 2)
            if rnd.random() < 0.01:
                amount = round(rnd.uniform(500_000, 1_500_000), 2)
            txn_n += 1
            db.session.add(Transaction(
                txn_ref=f"TXN{700000000000 + txn_n}", account_id=a.id, customer_id=a.customer_id,
                branch_id=a.branch_id, txn_type="Credit" if credit else "Debit",
                channel=rnd.choice(channels),
                category=("Deposit" if credit else rnd.choice(["Withdrawal", "Bill Payment", "Transfer Out"])),
                amount=amount, balance_after=round(a.balance, 2),
                description=("Cash deposit" if credit else "Withdrawal") if rnd.random() < 0.5 else "Funds transfer",
                flagged=amount >= 500_000,
                timestamp=datetime.combine(d, datetime.min.time()) + timedelta(
                    hours=rnd.randint(9, 17), minutes=rnd.randint(0, 59))))

    # Fixed deposits
    fd_n = 0
    for c in rnd.sample(customers, int(n_customers * 0.55)):
        for _ in range(rnd.choice([1, 1, 2])):
            fd_n += 1
            months = rnd.choice([6, 12, 24, 36, 60])
            start = today - timedelta(days=rnd.randint(0, 900))
            principal = rnd.choice([25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000])
            rate = {6: 6.0, 12: 6.8, 24: 7.0, 36: 7.1, 60: 6.5}[months]
            maturity = add_months(start, months)
            db.session.add(FixedDeposit(
                fd_number=f"FD{90000000 + fd_n}", customer=c, branch_id=c.branch_id,
                principal=principal, interest_rate=rate, tenure_months=months, start_date=start,
                maturity_date=maturity, maturity_amount=fd_maturity(principal, rate, months),
                status="Matured" if maturity < today else "Active",
                created_at=datetime.combine(start, datetime.min.time())))

    # Loans
    loan_specs = {"gold": (20_000, 800_000, 9.5, [6, 12, 24]),
                  "personal": (50_000, 1_000_000, 13.5, [12, 24, 36, 60]),
                  "home": (1_500_000, 8_000_000, 8.6, [120, 180, 240]),
                  "vehicle": (300_000, 1_500_000, 9.8, [36, 48, 60]),
                  "business": (500_000, 5_000_000, 11.5, [24, 36, 60]),
                  "education": (200_000, 2_000_000, 10.2, [60, 84]),
                  "other": (50_000, 500_000, 12.0, [12, 24])}
    loans = []
    loan_n = 0
    for c in rnd.sample(customers, int(n_customers * 0.7)):
        for _ in range(rnd.choice([1, 1, 2])):
            loan_n += 1
            ltype = rnd.choices(list(loan_specs), [30, 20, 10, 12, 10, 8, 10])[0]
            lo, hi, rate, tenures = loan_specs[ltype]
            amount = round(rnd.uniform(lo, hi), -3)
            months = rnd.choice(tenures)
            status = rnd.choices(["Active", "Overdue", "Closed", "Pending"], [62, 14, 16, 8])[0]
            disbursed = today - timedelta(days=rnd.randint(40, min(months * 30, 1000)))
            emi = calc_emi(amount, rate, months)
            c_accounts = [a for a in accounts if a.customer_id == c.id and a.status != "Closed"]
            loan = Loan(loan_number=f"LN{ltype[:2].upper()}{60000000 + loan_n}", customer=c,
                        account_id=c_accounts[0].id if c_accounts else None, branch_id=c.branch_id,
                        loan_type=ltype, amount=amount, interest_rate=rate,
                        tenure_months=months, emi=emi, status=status,
                        purpose={"gold": "Agricultural / personal needs", "home": "House purchase",
                                 "vehicle": "Four-wheeler purchase", "business": "Working capital",
                                 "education": "Higher studies", "personal": "Personal expenses",
                                 "other": "Consumer durable"}[ltype],
                        created_by=staff_of(c.branch, "Officer").id,
                        created_at=datetime.combine(disbursed, datetime.min.time()))
            if ltype == "gold":
                grams = round(amount / 4800 + rnd.uniform(0, 20), 1)
                loan.gold_weight_grams = grams
                loan.gold_purity = rnd.choice(["22K", "22K", "24K", "18K"])
                loan.gold_items = rnd.choice(["Chain, 2 bangles", "Necklace", "Ring, earrings, chain",
                                              "4 bangles", "Coins (5 nos)"])
                loan.gold_valuation = round(grams * 6200, -2)
            elif ltype == "home":
                loan.collateral = "Residential property — mortgage"
            elif ltype == "vehicle":
                loan.collateral = "Hypothecation of vehicle"
            if status == "Pending":
                loan.outstanding = 0
                loan.created_at = now - timedelta(days=rnd.randint(0, 6))
            else:
                loan.disbursed_on = disbursed
                paid_months = 0
                due = add_months(disbursed, 1)
                outstanding = amount
                limit_date = today if status != "Overdue" else today - timedelta(days=rnd.randint(35, 120))
                while due <= limit_date and outstanding > 0 and paid_months < months:
                    interest = round(outstanding * rate / 1200, 2)
                    principal_part = round(min(emi - interest, outstanding), 2)
                    outstanding = round(outstanding - principal_part, 2)
                    late = rnd.random() < 0.08
                    db.session.add(LoanRepayment(
                        loan=loan, due_date=due,
                        paid_on=min(due + timedelta(days=rnd.randint(4, 15) if late else 0), today),
                        amount=round(principal_part + interest, 2), principal_component=principal_part,
                        interest_component=interest, penalty=250 if late else 0,
                        mode=rnd.choice(["Auto-debit", "Cash", "NEFT", "UPI"]),
                        status="Late" if late else "Paid"))
                    paid_months += 1
                    due = add_months(due, 1)
                if status == "Closed":
                    loan.outstanding = 0
                    loan.next_due_date = None
                else:
                    loan.outstanding = max(outstanding, 0)
                    loan.next_due_date = due
                    if status == "Overdue":
                        loan.overdue_days = (today - due).days
            db.session.add(loan)
            loans.append(loan)
    db.session.flush()

    # Documents / KYC
    for c in customers:
        roll = rnd.random()
        for dtype in ["PAN Card", "Aadhaar", rnd.choice(["Passport", "Driving Licence", "Voter ID"])]:
            expiry = None
            if dtype in ("Passport", "Driving Licence"):
                expiry = today + timedelta(days=rnd.choice([rnd.randint(-40, 25), rnd.randint(200, 3000)]))
            status = "Pending" if roll < 0.15 else "Verified"
            if expiry and expiry < today:
                status = "Expired"
            doc = Document(customer=c, category="KYC", doc_type=dtype, file_name=f"{dtype.replace(' ', '_').lower()}.pdf",
                           issue_date=today - timedelta(days=rnd.randint(300, 3000)), expiry_date=expiry,
                           status=status, uploaded_by=staff_of(c.branch, "Clerk").id,
                           uploaded_at=c.created_at + timedelta(days=1),
                           verified_by=staff_of(c.branch, "Senior Officer").id if status != "Pending" else None,
                           verified_at=c.created_at + timedelta(days=2) if status != "Pending" else None)
            doc.doc_number = c.pan if dtype == "PAN Card" else str(rnd.randint(10**11, 10**12 - 1))
            db.session.add(doc)
        c.kyc_status = "Pending" if roll < 0.15 else "Verified"
    for loan in loans:
        if loan.loan_type in ("home", "vehicle", "gold", "business"):
            dtype = {"home": "Property Papers", "vehicle": "Vehicle RC", "gold": "Gold Valuation Certificate",
                     "business": "Income Proof"}[loan.loan_type]
            for t in ("Loan Agreement", dtype):
                d = Document(customer_id=loan.customer_id, loan=loan, category="Loan", doc_type=t,
                             file_name=f"{t.replace(' ', '_').lower()}_{loan.loan_number}.pdf",
                             status="Pending" if loan.status == "Pending" else "Verified",
                             uploaded_at=loan.created_at)
                d.doc_number = loan.loan_number
                db.session.add(d)
    db.session.flush()
    for c in customers:
        if any(d.status == "Expired" for d in c.documents):
            c.kyc_status = "Expired"

    # Approvals for pending loans (some already checked so the manager can decide)
    for loan in [l for l in loans if l.status == "Pending"]:
        maker = staff_of(loan.branch, "Officer")
        checked = rnd.random() < 0.5
        req = ApprovalRequest(
            request_number=f"APR{30000000 + loan.id}", request_type="loan_sanction",
            module="loan_approval", ref_type="loan", ref_id=loan.id, customer_id=loan.customer_id,
            branch_id=loan.branch_id, amount=loan.amount,
            summary=f"{loan.type_label} of ₹{loan.amount:,.0f} for {loan.customer.name}",
            payload_json="{}", maker_id=maker.id, created_at=loan.created_at,
            status="Reviewed" if checked else "Pending Review")
        if checked:
            req.checker_id = staff_of(loan.branch, "Senior Officer").id
            req.checker_remarks = "Documents verified, CIBIL score acceptable."
            req.checked_at = loan.created_at + timedelta(hours=5)
        db.session.add(req)

    # Tickets
    statuses = ["Open", "Assigned", "In Progress", "Pending", "Resolved", "Closed"]
    for i in range(45):
        c = rnd.choice(customers)
        subj, cat = rnd.choice(TICKET_SUBJECTS)
        st = rnd.choices(statuses, [15, 15, 20, 10, 20, 20])[0]
        created = now - timedelta(days=rnd.randint(0, 60), hours=rnd.randint(0, 20))
        t = Ticket(ticket_number=f"TKT{50000 + i}", customer=c, branch_id=c.branch_id, subject=subj,
                   description=f"Customer reported: {subj.lower()}.", category=cat,
                   priority=rnd.choice(["Low", "Medium", "Medium", "High"]), status=st,
                   assigned_to=staff_of(c.branch, "Officer").id if st != "Open" else None,
                   created_by=staff_of(c.branch, "Clerk").id, created_at=created,
                   updated_at=created + timedelta(hours=6),
                   resolved_at=created + timedelta(days=1) if st in ("Resolved", "Closed") else None)
        db.session.add(t)
        db.session.flush()
        db.session.add(TicketNote(ticket=t, employee_id=t.created_by, note="Ticket logged at branch desk.",
                                  status_change="Open", timestamp=created))

    # Activity timeline & audit history
    for c in customers:
        db.session.add(Activity(customer=c, employee_id=staff_of(c.branch, "Officer").id,
                                action="Customer onboarded", reference=c.customer_code, timestamp=c.created_at))
        for a in [a for a in accounts if a.customer_id == c.id]:
            db.session.add(Activity(customer=c, employee_id=staff_of(c.branch, "Officer").id,
                                    action="Account created", reference=a.account_number,
                                    details=f"{a.account_type} account opened", timestamp=a.created_at))
        db.session.add(Activity(customer=c, employee_id=staff_of(c.branch, "Senior Officer").id,
                                action="KYC updated", reference=c.customer_code,
                                details=f"KYC status: {c.kyc_status}",
                                timestamp=c.created_at + timedelta(days=2)))
    for loan in loans:
        db.session.add(Activity(customer_id=loan.customer_id, employee_id=loan.created_by,
                                action="Loan created", reference=loan.loan_number,
                                details=f"{loan.type_label} ₹{loan.amount:,.0f}", timestamp=loan.created_at))
        last = loan.repayments.first() if loan.id else None
        if last:
            db.session.add(Activity(customer_id=loan.customer_id, action="Payment received",
                                    reference=loan.loan_number, details=f"EMI ₹{last.amount:,.0f}",
                                    timestamp=datetime.combine(last.paid_on, datetime.min.time()) + timedelta(hours=11)))
    for s in staff[1:]:
        db.session.add(AuditLog(employee_id=s.id, email=s.email, action="Login", module="auth",
                                ip_address="10.0.0." + str(rnd.randint(2, 200)),
                                timestamp=now - timedelta(hours=rnd.randint(1, 48))))
    db.session.commit()
    return True


def register_cli(app):
    @app.cli.command("init-db")
    @click.option("--demo/--no-demo", default=True, help="Load demo customers, loans, etc.")
    def init_db(demo):
        """Create tables, roles, the Super Admin and (optionally) demo data."""
        db.create_all()
        ensure_core()
        if demo and seed_demo():
            click.echo("Demo data loaded.")
        from .services import scan_scheduled_alerts
        scan_scheduled_alerts(force=True)
        click.echo(f"Ready. Super Admin: {SUPER_ADMIN_EMAIL}")

    @app.cli.command("reset-admin-password")
    @click.argument("password")
    def reset_admin(password):
        from .security import password_problems
        problems = password_problems(password)
        if problems:
            raise click.ClickException("Password needs " + ", ".join(problems))
        admin = ensure_core()
        admin.set_password(password)
        admin.failed_attempts = 0
        admin.locked_until = None
        admin.session_token = secrets.token_hex(16)
        db.session.commit()
        click.echo("Super Admin password updated.")
