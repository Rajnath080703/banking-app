"""Reference data: roles and permissions, risk rules, chart of accounts, one starting branch
and the first Super Admin. Everything here is idempotent."""
import os
import secrets

import click

from .extensions import db
from .models import ACTIONS, MODULES, AlertRule, Branch, Employee, Permission, Role

SUPER_ADMIN_EMAIL = os.environ.get("BANK_ADMIN_EMAIL", "rajnath080703@gmail.com")

ALL = set(ACTIONS)
V = {"view"}
VP = {"view", "print"}
VC = {"view", "create"}
VCE = {"view", "create", "edit"}
VCEAR = {"view", "create", "edit", "approve", "reject"}
VX = {"view", "export"}

ROLE_MATRIX = {
    "Super Admin": (100, "Full access to every module and branch.", {m: ALL for m, _ in MODULES}),
    "Bank Admin": (90, "Head-office administrator: all branches, staff, roles and settings; no money movement.", {
        "user_management": VCEAR | {"delete"}, "branch_management": VCEAR, "settings": VCE,
        "customer_accounts": V, "customer_profile": V, "transactions": V, "loans": V, "deposits": V, "kyc": V,
        "tickets": V, "reports": VX | {"print"}, "audit_logs": VX, "alerts": VCE, "accounting": VX, "interest": V,
    }),
    "Branch Manager": (80, "Runs a branch; final approver in maker-checker.", {
        "user_management": V, "customer_accounts": VCEAR | {"export", "print"},
        "customer_profile": VCEAR | {"export"}, "transactions": VC | {"approve", "reject", "export", "print"},
        "accounting": VX | {"print"}, "loans": VCE | {"export", "print"},
        "loan_approval": {"view", "approve", "reject"}, "interest": VCEAR,
        "deposits": VCEAR | {"export", "print"}, "kyc": VCEAR | {"export"},
        "tickets": VCE | {"delete", "export"}, "reports": VX | {"print"},
        "branch_management": {"view", "edit"}, "audit_logs": VX, "alerts": VCE, "settings": V,
    }),
    "Senior Officer": (60, "Checker for loans, accounts, deposits and KYC.", {
        "customer_accounts": VCEAR | {"print"}, "customer_profile": VCEAR, "transactions": VC | {"approve", "reject", "print"},
        "accounting": V, "loans": VCE | {"print"}, "loan_approval": {"view", "approve", "reject"},
        "interest": V, "deposits": VCEAR | {"print"}, "kyc": VCEAR, "tickets": VCE,
        "reports": VX | {"print"}, "branch_management": V, "alerts": VCE,
    }),
    "Accountant": (55, "Books and reconciliation: ledger, trial balance, interest runs (maker).", {
        "accounting": VX | {"print", "create"}, "interest": VC, "transactions": VX | {"print"},
        "customer_accounts": V, "loans": V, "deposits": V, "reports": VX | {"print"}, "alerts": V,
    }),
    "Auditor": (50, "Internal / statutory auditor: read everything, export reports and audit logs, change nothing.", {
        **{m: V for m, _ in MODULES if m not in ("settings",)},
        "reports": VX, "audit_logs": VX, "transactions": VX, "accounting": VX,
    }),
    "Loan Officer": (45, "Originates and services loans; maker for loan applications and repayments.", {
        "customer_profile": V, "customer_accounts": V, "loans": VCE | {"print"}, "kyc": VC,
        "transactions": V, "interest": V, "reports": V, "alerts": V, "tickets": VC,
    }),
    "Officer": (40, "Maker for accounts, loans, deposits; handles customers.", {
        "customer_accounts": VC | {"print"}, "customer_profile": VC, "transactions": VC | {"print"}, "loans": VC,
        "interest": V, "deposits": VC, "kyc": VC, "tickets": VCE, "reports": V, "alerts": V,
    }),
    "Clerk": (20, "Front desk: lookups, cash transactions, document intake.", {
        "customer_accounts": V, "transactions": VC, "kyc": VC, "tickets": VC, "alerts": V,
    }),
    "Read-Only": (10, "Look up customers, accounts, loans and deposits without changing anything.", {
        "customer_accounts": V, "customer_profile": V, "transactions": V, "loans": V, "deposits": V, "kyc": V,
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
    """Idempotent: roles, rules, chart of accounts, a branch and the first super admin."""
    from .ledger import ensure_chart
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
        # One starting branch so the Super Admin has a home branch; rename it in Settings.
        db.session.add(Branch(code="MAIN001", name="Main Branch", city="",
                              target_deposits=0, target_loans=0, target_accounts=0))

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
            AlertRule(name="Sign-in from a new network", rule_type="new_login_ip", threshold=0,
                      severity="Medium", route_module="user_management",
                      description="An employee signs in from an IP address not used in the last 30 days."),
        ])
    ensure_chart()
    from .charges import ensure_fee_types
    ensure_fee_types()
    db.session.flush()

    admin = Employee.query.filter_by(email=SUPER_ADMIN_EMAIL).first()
    if not admin:
        initial = os.environ.get("BANK_ADMIN_PASSWORD")
        if not initial:
            raise RuntimeError("Set BANK_ADMIN_PASSWORD in .env to create the first Super Admin account.")
        admin = Employee(employee_code="EMP0001", name="Rajnath", email=SUPER_ADMIN_EMAIL,
                         role=Role.query.filter_by(name="Super Admin").one(),
                         branch=Branch.query.order_by(Branch.id).first(),
                         session_token=secrets.token_hex(16))
        admin.set_password(initial)
        db.session.add(admin)
    db.session.commit()
    return admin


def register_cli(app):
    @app.cli.command("prepare-db")
    def prepare_db():
        """Create the runtime DB role, apply migrations as the owner, and seed reference data."""
        from .bootstrap import prepare_database
        prepare_database()
        click.echo(f"Database ready. Super Admin: {SUPER_ADMIN_EMAIL}")

    @app.cli.command("scan")
    def scan():
        """Run the overdue / expiry / FD-maturity scan and seal the audit log (schedule with cron)."""
        from .services import scan_scheduled_alerts
        scan_scheduled_alerts(force=True)
        click.echo("Scan complete.")

    @app.cli.command("verify-audit")
    def verify_audit():
        """Recompute the audit hash chain and report any break."""
        from .audit_chain import seal, verify
        seal()
        result = verify()
        click.echo(result)
        if not result["ok"]:
            raise SystemExit(1)

    @app.cli.command("reconcile")
    def reconcile():
        """Check that product balances agree with the general ledger."""
        from .ledger import reconciliation
        bad = 0
        for c in reconciliation():
            click.echo(f"{'OK ' if c['ok'] else 'BAD'}  {c['name']}: ledger {c['ledger']} / books {c['books']}")
            bad += 0 if c["ok"] else 1
        if bad:
            raise SystemExit(1)

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
