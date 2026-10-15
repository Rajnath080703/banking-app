"""Backend permission sweep: every system role × every page, compared with the role's granted
rights, plus direct POSTs for actions the role lacks. Nothing here relies on hidden buttons."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import M, cleanup, form_token, idem, invite_employee, q, setup_bank  # noqa: E402

from bank.seed import ROLE_MATRIX  # noqa: E402

PAGES = [  # (url, module, action)
    ("/customers/", "customer_profile", "view"), ("/customers/new", "customer_profile", "create"),
    ("/accounts/", "customer_accounts", "view"), ("/accounts/new", "customer_accounts", "create"),
    ("/loans/", "loans", "view"), ("/loans/gold", "loans", "view"), ("/loans/new", "loans", "create"),
    ("/deposits/", "deposits", "view"), ("/deposits/new", "deposits", "create"),
    ("/transactions/", "transactions", "view"), ("/transactions/new", "transactions", "create"),
    ("/kyc/", "kyc", "view"), ("/kyc/upload", "kyc", "create"),
    ("/tickets/", "tickets", "view"), ("/tickets/new", "tickets", "create"),
    ("/reports/", "reports", "view"), ("/alerts/", "alerts", "view"), ("/audit/", "audit_logs", "view"),
    ("/users/", "user_management", "view"), ("/users/new", "user_management", "create"),
    ("/users/login-history", "user_management", "view"), ("/settings/", "settings", "view"),
    ("/branch", "branch_management", "view"), ("/accounting/", "accounting", "view"),
    ("/accounting/journal", "accounting", "view"), ("/accounting/interest", "interest", "view"),
]

try:
    S, cid, aid, acc_no = setup_bank()
    admin = S["admin"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)
    clients = {}
    for name in ROLE_MATRIX:
        if name == "Super Admin":
            continue
        email = name.lower().replace(" ", "").replace("-", "") + "@perm.test"
        clients[name] = invite_employee(admin, name + " Sweep", email, roles[name], b1)
    problems, checked = [], 0
    for name, c in clients.items():
        granted = ROLE_MATRIX[name][2]
        for url, module, action in PAGES:
            allowed = action in granted.get(module, set())
            code = c.get(url).status_code
            checked += 1
            if (code == 200) != allowed or code not in (200, 403):
                problems.append((name, url, f"expected {'200' if allowed else '403'}", f"got {code}"))
        # exports need reports.export; statements need transactions.print
        for url, need in (("/reports/loans?export=xlsx", ("reports", "export")),
                          (f"/customers/{cid}/statement", ("transactions", "print"))):
            module, action = need
            can = action in granted.get(module, set()) and "view" in granted.get("loans" if "reports" in url else "customer_profile", set())
            if url.startswith("/customers") and "view" not in granted.get("customer_profile", set()):
                can = False
            code = c.get(url).status_code
            checked += 1
            if (code == 200) != can:
                problems.append((name, url, f"expected {'200' if can else '403'}", f"got {code}"))
        # direct POSTs to money / admin actions the role does not hold
        for url, module, action in (("/loans/new", "loans", "create"), (f"/accounts/{aid}/close", "customer_accounts", "edit"),
                                    ("/users/new", "user_management", "create"), ("/settings/", "settings", "view"),
                                    (f"/transactions/new?account={acc_no}", "transactions", "create")):
            if action in granted.get(module, set()):
                continue
            t, html = form_token(c, "/")
            code = c.post(url, data={"_csrf": t, "_idem": idem(html)}).status_code
            checked += 1
            if code != 403:
                problems.append((name, "POST " + url, "expected 403", f"got {code}"))
    print(f"permission sweep: {checked} role/page checks across {len(clients)} roles, {len(problems)} mismatches")
    for p in problems:
        print("  MISMATCH", p)
finally:
    cleanup()
sys.exit(1 if problems else 0)
