"""End-to-end: the full employee journey on an empty database, then every page and export.

LOGIN → DASHBOARD → CUSTOMER SEARCH → CUSTOMER 360 → ACCOUNT → TRANSACTION → LOAN → REPAYMENT
→ KYC → DOCUMENT → APPROVAL → REPORT → LOGOUT"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (ADMIN, M, app, approve_latest, balance, cleanup, client, db, deposit, post, q,  # noqa: E402
                     setup_bank, upload)

results = []


def check(title, ok, detail=""):
    results.append((ok, title))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


try:
    admin = client(*ADMIN)
    empty = ["/", "/?range=today", "/?range=week", "/?range=year", "/branch", "/customers/", "/accounts/", "/loans/",
             "/loans/gold", "/loans/type/home", "/deposits/", "/transactions/", "/kyc/", "/tickets/", "/approvals/",
             "/reports/", "/alerts/", "/alerts/rules", "/audit/", "/users/", "/users/new", "/users/roles", "/settings/",
             "/accounting/", "/accounting/journal", "/accounting/interest", "/customers/search?q=nothing"]
    bad = [(p, admin.get(p).status_code) for p in empty if admin.get(p).status_code != 200]
    check(f"Empty database: {len(empty)} pages render", not bad, bad)

    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr = S["officer"], S["senior"], S["manager"]
    check("Search by PAN finds the customer (redirects to Customer 360)",
          admin.get("/customers/?q=ABCDE1234F").status_code == 302)
    check("Search by mobile and account number", admin.get("/customers/?q=9876543210").status_code == 302 and
          admin.get(f"/customers/?q={acc_no}").status_code == 302)

    deposit(off, acc_no, "250000")
    deposit(off, acc_no, "600000")
    deposit(off, acc_no, "5000", "Debit")
    post(off, "/loans/new", {"customer": cid, "loan_type": "gold", "account_id": aid, "amount": "200000",
                             "interest_rate": "9.5", "tenure_months": "12", "gold_weight_grams": "45",
                             "gold_purity": "22K", "gold_valuation": "280000"}, f"/loans/new?customer={cid}")
    approve_latest(S)
    lid = q(lambda: M.Loan.query.one().id)
    emi = q(lambda: db.session.get(M.Loan, lid).emi)
    post(sen, f"/loans/{lid}/repay", {"amount": str(emi), "mode": "Account debit"}, f"/loans/{lid}")
    post(off, "/deposits/new", {"customer": cid, "account_id": aid, "principal": "50000", "tenure_months": "12"},
         f"/deposits/new?customer={cid}")
    approve_latest(S)
    post(off, "/tickets/new", {"customer": cid, "subject": "Cheque book request", "category": "Services", "priority": "Low"},
         f"/tickets/new?customer={cid}")
    upload(admin, cid, "Address Proof", "AP1")
    expected = 250000 + 600000 - 5000 + 200000 - float(emi) - 50000
    check("Balance after deposits, loan disbursal, EMI debit and FD booking", float(balance(aid)) == round(expected, 2),
          f"{balance(aid)} expected {expected:.2f}")

    from bank.views.reports import REPORTS
    pages = ["/", "/?range=week", "/?range=year", "/?range=custom&start=2026-01-01&end=2026-12-31", "/branch",
             f"/customers/{cid}", f"/loans/{lid}", "/loans/gold", f"/loans/gold?q={acc_no}", "/accounts/?sort=balance",
             "/loans/?sort=outstanding&dir=asc", "/deposits/", "/transactions/?range=year", "/transactions/?flagged=1",
             "/kyc/?view=verified", "/tickets/", "/approvals/?status=all", "/alerts/", "/audit/?range=year",
             "/users/login-history", "/accounting/", "/accounting/journal", f"/customers/{cid}/statement",
             f"/customers/{cid}/edit"]
    for tab in ["accounts", "loans", "deposits", "transactions", "documents", "timeline", "repayments", "approvals",
                "tickets", "notifications"]:
        pages.append(f"/customers/{cid}?tab={tab}")
    for k in REPORTS:
        pages += [f"/reports/{k}?range=year", f"/reports/{k}?range=year&export=xlsx", f"/reports/{k}?range=year&export=pdf"]
    fails = [(p, admin.get(p).status_code) for p in pages if admin.get(p).status_code != 200]
    check(f"Populated database: {len(pages)} pages and exports load", not fails, fails[:5])

    with app.app_context():
        from bank.ledger import reconciliation
        check("Books reconcile after the journey", all(c["ok"] for c in reconciliation()))
    t = admin.get("/").get_data(as_text=True)
    import re
    tok = re.search(r'name="_csrf" value="([^"]+)"', t).group(1)
    r = admin.post("/logout", data={"_csrf": tok}, follow_redirects=False)
    check("Logout ends the session", r.status_code == 302 and admin.get("/", follow_redirects=False).status_code == 302)
finally:
    cleanup()

failed = [t for ok, t in results if not ok]
print(f"\nend-to-end: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
