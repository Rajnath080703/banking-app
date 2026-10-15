"""Sample data for the dev environment only: `flask seed-demo`.

Refuses to run unless BANK_DEMO_DATA_ALLOWED=1 (set only in the dev environment) and the database
has no customers yet. Sample staff are created directly; everything else goes through the app's
own screens (KYC, maker-checker approvals, ledger postings), so every business rule applies.
All names, PANs and mobile numbers are made up."""
import io
import os
import re

import click

DEMO_STAFF = [  # (key, name, email local part, role)
    ("officer", "Demo Officer", "officer", "Officer"),
    ("senior", "Demo Senior Officer", "senior", "Senior Officer"),
    ("manager", "Demo Branch Manager", "manager", "Branch Manager"),
    ("clerk", "Demo Clerk", "clerk", "Clerk"),
]
DEMO_CUSTOMERS = [  # (name, pan, mobile, gender, onboard fully?)
    ("Priya Raman", "PRIYA1234K", "9840011122", "Female", True),
    ("Arjun Mehta", "ARJUN5678L", "9840022233", "Male", True),
    ("Lakshmi Narayanan", "LAKSH9012M", "9840033344", "Female", True),
    ("Suresh Kumar", "SURES3456N", "9840044455", "Male", True),
    ("Fatima Begum", "FATIM7890P", "9840055566", "Female", False),
]


class _Client:
    """The app's test client over https (the session cookie is Secure in this environment)."""

    def __init__(self, app, email, password):
        self.c = app.test_client()
        r = self.c.post("/login", base_url="https://localhost",
                        data={"email": email, "password": password, "_csrf": self._tok(self.get("/login"))})
        if r.status_code != 302:
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get_data(as_text=True)))[:300]
            raise click.ClickException(f"Sample staff {email} could not sign in (status {r.status_code}): {text}")

    def get(self, url):
        return self.c.get(url, base_url="https://localhost").get_data(as_text=True)

    @staticmethod
    def _tok(html):
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        return m.group(1) if m else ""

    def post(self, url, data, form_url=None, files=None):
        html = self.get(form_url or url)
        m = re.search(r'name="_idem" value="([^"]+)"', html)
        payload = dict(data, _csrf=self._tok(html), _idem=m.group(1) if m else "")
        kw = {"content_type": "multipart/form-data"} if files else {}
        if files:
            payload.update(files)
        return self.c.post(url, data=payload, base_url="https://localhost", follow_redirects=True, **kw)


def register(app):
    # Not run inside the CLI's app context: the requests below must each get their own context,
    # as in a real server, so database reads are wrapped in short contexts of their own.
    @app.cli.command("seed-demo", with_appcontext=False)
    def seed_demo():
        """Load sample customers, accounts, loans and FDs (dev environment only)."""
        from .extensions import db
        from .models import Branch, Customer, Employee, Role

        def q(fn):
            with app.app_context():
                return fn()

        if os.environ.get("BANK_DEMO_DATA_ALLOWED") != "1":
            raise click.ClickException("Refused: sample data is allowed only where BANK_DEMO_DATA_ALLOWED=1 (dev).")
        if q(lambda: Customer.query.count()):
            raise click.ClickException("Refused: this database already has customers.")
        password = os.environ.get("BANK_DEMO_STAFF_PASSWORD", "")
        from .security import password_problems
        if password_problems(password):
            raise click.ClickException("Set BANK_DEMO_STAFF_PASSWORD to a strong password for the sample staff.")
        domain = os.environ.get("BANK_DEMO_EMAIL_DOMAIN", "demo.bank.test")
        with app.app_context():
            branch_id = Branch.query.order_by(Branch.id).first().id
            roles = {r.name: r.id for r in Role.query.all()}
            for i, (_key, name, local, role) in enumerate(DEMO_STAFF, 1):  # safe to re-run
                emp = Employee.query.filter_by(email=f"{local}@{domain}").first() or Employee(
                    employee_code=f"DEMO{i:03d}", email=f"{local}@{domain}")
                emp.name, emp.role_id, emp.branch_id = name, roles[role], branch_id
                emp.is_active_flag, emp.failed_attempts, emp.locked_until = True, 0, None
                emp.set_password(password)
                db.session.add(emp)
            db.session.commit()
        result = {}

        def run():  # fresh thread = no inherited app context, so every request gets its own (as on a server)
            try:
                result["ok"] = _load(app, q, domain, password, branch_id)
            except BaseException as exc:  # noqa: BLE001 - re-raised below in the CLI thread
                result["error"] = exc

        import threading
        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        if "error" in result:
            raise result["error"]
        if not result.get("ok"):
            raise SystemExit(1)


def _load(app, q, domain, password, branch_id):
    """The sample-data steps, performed through the app's own screens. Returns True if the books reconcile."""
    from .models import Account, ApprovalRequest, Customer, Document, Loan
    S = {key: _Client(app, f"{local}@{domain}", password) for key, _, local, _ in DEMO_STAFF}

    def approve_latest():
        rid = q(lambda: ApprovalRequest.query.filter_by(status="Pending Review")
                .order_by(ApprovalRequest.id.desc()).first().id)
        S["senior"].post(f"/approvals/{rid}", {"action": "review", "remarks": "Checked"})
        S["manager"].post(f"/approvals/{rid}", {"action": "approve", "remarks": "Approved"})

    for i, (name, pan, mobile, gender, full) in enumerate(DEMO_CUSTOMERS):
        S["officer"].post("/customers/new", {"name": name, "pan": pan, "mobile": mobile, "branch_id": branch_id,
                                             "gender": gender, "city": "Chennai", "occupation": "Salaried"})
        cid = q(lambda n=name: Customer.query.filter_by(name=n).one().id)
        docs = [("PAN Card", pan), ("Aadhaar", f"55556666771{i}")] if full else [("PAN Card", pan), ("Aadhaar", "777788889999")]
        for doc_type, number in docs:
            S["officer"].post("/kyc/upload", {"customer": cid, "doc_type": doc_type, "doc_number": number},
                              f"/kyc/upload?customer={cid}",
                              files={"file": (io.BytesIO(b"%PDF-1.4 sample scan"), "scan.pdf")})
        if not full:
            continue  # left with KYC waiting for verification
        for did in q(lambda c=cid: [d.id for d in Document.query.filter_by(customer_id=c)]):
            S["senior"].post(f"/kyc/doc/{did}/verify", {"decision": "approve"}, f"/kyc/doc/{did}")
        S["officer"].post("/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
        approve_latest()
        acc_id, acc_no = q(lambda c=cid: (lambda a: (a.id, a.account_number))(Account.query.filter_by(customer_id=c).one()))
        S["officer"].post(f"/transactions/new?account={acc_no}",
                          {"txn_type": "Credit", "amount": str(50000 * (i + 2)), "channel": "Cash"})
        loan = {0: ("personal", "200000", "11.5", "24", {}),
                1: ("gold", "90000", "9.5", "12", {"gold_weight_grams": "25", "gold_purity": "22K", "gold_valuation": "150000"})}
        if i in loan:
            lt, amt, rate, tenure, extra = loan[i]
            S["officer"].post("/loans/new", dict({"customer": cid, "loan_type": lt, "account_id": acc_id, "amount": amt,
                                                  "interest_rate": rate, "tenure_months": tenure}, **extra),
                              f"/loans/new?customer={cid}")
            approve_latest()
        if i == 2:
            S["officer"].post("/deposits/new", {"customer": cid, "account_id": acc_id, "principal": "50000",
                                                "tenure_months": "12"}, f"/deposits/new?customer={cid}")
            approve_latest()
        if i == 3:  # left waiting for approval, so the queues have work in them
            S["officer"].post("/loans/new", {"customer": cid, "loan_type": "home", "account_id": acc_id,
                                             "amount": "1500000", "interest_rate": "8.75", "tenure_months": "180"},
                              f"/loans/new?customer={cid}")
    first = q(lambda: Customer.query.filter_by(name=DEMO_CUSTOMERS[0][0]).one().id)
    S["clerk"].post("/tickets/new", {"customer": first, "subject": "Cheque book not received", "category": "Services",
                                     "priority": "Medium"}, f"/tickets/new?customer={first}")
    from .ledger import reconciliation
    ok, counts = q(lambda: (all(c["ok"] for c in reconciliation()),
                            (Customer.query.count(), Account.query.count(), Loan.query.count())))
    click.echo(f"Sample data loaded: {counts[0]} customers, {counts[1]} accounts, {counts[2]} loans. "
               f"Books reconcile: {ok}.")
    click.echo("Sample staff (password = BANK_DEMO_STAFF_PASSWORD): "
               + ", ".join(f"{local}@{domain} ({role})" for _, _, local, role in DEMO_STAFF))
    return ok
