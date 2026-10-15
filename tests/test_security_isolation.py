"""Security and isolation: branch isolation on every record-id route, CSRF, XSS, SQL injection,
concurrent withdrawals, sensitive data in logs and URLs, security headers, error leakage."""
import logging
import os
import sys
import threading
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, PW, app, approve_latest, balance, cleanup, client, db, deposit, form_token,  # noqa: E402
                     idem, invite_employee, post, q, setup_bank, upload)

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin = S["officer"], S["senior"], S["manager"], S["admin"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})

    # ---------------- a second branch with its own staff, customer and records -----------
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    S2 = {"admin": admin,
          "officer": invite_employee(admin, "Officer Two", "officer2@bank.test", roles["Officer"], b2),
          "senior": invite_employee(admin, "Senior Two", "senior2@bank.test", roles["Senior Officer"], b2),
          "manager": invite_employee(admin, "Manager Two", "managerb2@bank.test", roles["Branch Manager"], b2)}
    post(admin, "/customers/new", {"name": "Branch Two Customer", "pan": "BRTWO1234C", "mobile": "9700000002",
                                   "branch_id": b2, "gender": "Male"})
    cid2 = q(lambda: M.Customer.query.filter_by(pan_hash=M.Customer.query.filter_by(name="Branch Two Customer").one().pan_hash).one().id)
    for dt, no in (("PAN Card", "BRTWO1234C"), ("Aadhaar", "222233334444")):
        upload(admin, cid2, dt, no)
    for d in q(lambda: [d.id for d in M.Document.query.filter_by(customer_id=cid2)]):
        post(S2["senior"], f"/kyc/doc/{d}/verify", {"decision": "approve"}, f"/kyc/doc/{d}")
    post(S2["officer"], "/accounts/new", {"customer": cid2, "account_type": "Savings"}, f"/accounts/new?customer={cid2}")
    approve_latest(S2)
    acc2 = q(lambda: M.Account.query.filter_by(customer_id=cid2).one())
    deposit(S2["officer"], acc2.account_number, "90000")
    post(S2["officer"], "/loans/new", {"customer": cid2, "loan_type": "personal", "account_id": acc2.id, "amount": "50000",
                                       "interest_rate": "12", "tenure_months": "12"}, f"/loans/new?customer={cid2}")
    approve_latest(S2)
    lid2 = q(lambda: M.Loan.query.filter_by(customer_id=cid2).one().id)
    post(S2["officer"], "/deposits/new", {"customer": cid2, "account_id": acc2.id, "principal": "10000", "tenure_months": "6"},
         f"/deposits/new?customer={cid2}")
    approve_latest(S2)
    fid2 = q(lambda: M.FixedDeposit.query.filter_by(customer_id=cid2).one().id)
    post(S2["officer"], "/tickets/new", {"customer": cid2, "subject": "Branch two ticket", "category": "Services",
                                         "priority": "Low"}, f"/tickets/new?customer={cid2}")
    tid2 = q(lambda: M.Ticket.query.filter_by(customer_id=cid2).one().id)
    did2 = q(lambda: M.Document.query.filter_by(customer_id=cid2).first().id)
    post(S2["senior"], f"/customers/{cid2}/edit", {"mobile": "9700000003", "email": "", "address": "", "city": "",
                                                     "occupation": "", "risk_category": "Low"})
    rid2 = q(lambda: M.ApprovalRequest.query.filter_by(customer_id=cid2, request_type="customer_update").one().id)

    # ---------------- branch isolation: every record-id route, read and write ----------
    reads = [f"/customers/{cid2}", f"/customers/{cid2}?tab=accounts", f"/customers/{cid2}/statement", f"/customers/{cid2}/edit",
             f"/loans/{lid2}", f"/deposits/{fid2}/close", f"/kyc/doc/{did2}", f"/kyc/doc/{did2}/file", f"/approvals/{rid2}",
             f"/tickets/{tid2}", f"/transactions/new?account={acc2.account_number}", f"/accounts/new?customer={cid2}",
             f"/loans/new?customer={cid2}", f"/deposits/new?customer={cid2}", f"/kyc/upload?customer={cid2}",
             f"/tickets/new?customer={cid2}"]
    leaks = []
    for who in ("officer", "senior", "manager"):
        c = S[who]
        for url in reads:
            r = c.get(url)
            body = r.get_data(as_text=True)
            if r.status_code == 200 and ("Branch Two Customer" in body or "BRTWO" in body):
                leaks.append((who, url, r.status_code))
    check("Branch-1 staff cannot open any branch-2 record by id or reference", not leaks, leaks[:4])

    writes = [(f"/accounts/{acc2.id}/freeze", {"reason": "x"}), (f"/accounts/{acc2.id}/close", {}),
              (f"/loans/{lid2}/repay", {"amount": "100", "mode": "Cash"}), (f"/loans/{lid2}/rate", {"new_rate": "20", "reason": "x"}),
              (f"/deposits/{fid2}/close", {"reason": "x"}), (f"/kyc/doc/{did2}/verify", {"decision": "reject", "remarks": "x"}),
              (f"/approvals/{rid2}", {"action": "review", "remarks": "x"}), (f"/customers/{cid2}/reveal", {}),
              (f"/tickets/{tid2}", {"status": "Closed"}),
              (f"/transactions/new?account={acc2.account_number}", {"txn_type": "Debit", "amount": "100", "channel": "Cash"})]
    before = q(lambda: (db.session.get(M.Account, acc2.id).balance, db.session.get(M.Account, acc2.id).status,
                        db.session.get(M.Loan, lid2).outstanding, db.session.get(M.FixedDeposit, fid2).status,
                        db.session.get(M.ApprovalRequest, rid2).status, db.session.get(M.Ticket, tid2).status,
                        M.ApprovalRequest.query.count()))
    codes = []
    for url, data in writes:
        t, html = form_token(mgr, "/")
        codes.append(mgr.post(url, data=dict(data, _csrf=t, _idem=idem(html))).status_code)
    after = q(lambda: (db.session.get(M.Account, acc2.id).balance, db.session.get(M.Account, acc2.id).status,
                       db.session.get(M.Loan, lid2).outstanding, db.session.get(M.FixedDeposit, fid2).status,
                       db.session.get(M.ApprovalRequest, rid2).status, db.session.get(M.Ticket, tid2).status,
                       M.ApprovalRequest.query.count()))
    check("Branch-1 manager's direct POSTs to branch-2 records change nothing", before == after and all(c in (403, 404) for c in codes),
          f"codes {codes}")
    lists = {u: mgr.get(u).get_data(as_text=True) for u in ("/accounts/", "/loans/", "/deposits/", "/transactions/?range=year",
                                                            "/kyc/?view=all", "/tickets/", "/approvals/?status=all", "/customers/",
                                                            "/customers/search?q=BRTWO1234C", "/reports/loans?range=year")}
    check("Branch-2 records never appear in branch-1 lists, search or reports",
          not [u for u, b in lists.items() if "Branch Two Customer" in b], [u for u, b in lists.items() if "Branch Two Customer" in b])
    check("Super Admin (all branches) does see branch-2 records", "Branch Two Customer" in admin.get("/accounts/").get_data(as_text=True))

    # ---------------- CSRF -----------------------------------------------------------------
    b0 = balance(aid)
    r1 = off.post(f"/transactions/new?account={acc_no}", data={"txn_type": "Credit", "amount": "100", "channel": "Cash"})
    r2 = off.post(f"/transactions/new?account={acc_no}", data={"_csrf": "forged", "txn_type": "Credit", "amount": "100", "channel": "Cash"})
    t_other = form_token(sen, "/")[0]  # a real token, but from another employee's session
    r3 = off.post(f"/transactions/new?account={acc_no}", data={"_csrf": t_other, "txn_type": "Credit", "amount": "100", "channel": "Cash"})
    check("POST without, with a forged, or with another session's CSRF token is refused",
          (r1.status_code, r2.status_code, r3.status_code) == (400, 400, 400) and balance(aid) == b0,
          (r1.status_code, r2.status_code, r3.status_code))
    xs = off.post("/logout", data={}, headers={"Origin": "https://evil.example"})
    check("Sign-out cannot be triggered cross-site without a token", xs.status_code == 400, xs.status_code)

    # ---------------- XSS: hostile text is escaped everywhere it is shown --------------
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    post(admin, "/customers/new", {"name": evil, "pan": "XSSAB1234Z", "mobile": "9600000001", "branch_id": q(lambda: M.Branch.query.first().id),
                                   "gender": "Male", "address": evil, "occupation": evil})
    xcid = q(lambda: M.Customer.query.filter(M.Customer.name.like("<script>%")).one().id)
    post(off, "/tickets/new", {"customer": xcid, "subject": evil, "category": "Other", "priority": "Low", "description": evil},
         f"/tickets/new?customer={xcid}")
    pages = [f"/customers/{xcid}", f"/customers/{xcid}?tab=timeline", "/customers/", "/customers/search?q=XSSAB1234Z",
             "/tickets/", "/", "/audit/?range=year", "/reports/kyc-status?range=year", f"/accounts/new?customer={xcid}"]
    raw = [p for p in pages if "<script>alert(1)" in admin.get(p).get_data(as_text=True)
           or "<img src=x onerror" in admin.get(p).get_data(as_text=True)]
    check("Script in customer name, address and ticket text is rendered as text, never as HTML", not raw, raw)
    csp = admin.get("/").headers.get("Content-Security-Policy", "")
    check("Content-Security-Policy blocks inline scripts as a second line of defence",
          "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0])

    # ---------------- SQL injection probes -------------------------------------------------
    probes = ["' OR '1'='1", "'; DROP TABLE customer; --", "1 UNION SELECT password_hash FROM employee--", "%' OR 1=1 --"]
    bad = []
    for pr in probes:
        for url in (f"/customers/?q={pr}", f"/customers/search?q={pr}", f"/accounts/?q={pr}", f"/loans/?q={pr}",
                    f"/accounts/?sort={pr}", f"/loans/?sort={pr}&dir={pr}", f"/audit/?q={pr}", f"/reports/loans?range={pr}",
                    f"/transactions/new?account={pr}"):
            r = admin.get(url)
            if r.status_code >= 500 or "Audit Customer" in r.get_data(as_text=True) and "q=" in url and "Rajnath" not in pr:
                bad.append((url, r.status_code))
    alive = q(lambda: M.Customer.query.count())
    check("Injection strings in search, sort, filter and account fields are treated as data", not bad and alive >= 3, bad[:3])

    # ---------------- concurrent withdrawals cannot overdraw ---------------------------
    post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    approve_latest(S)
    race = q(lambda: M.Account.query.filter_by(customer_id=cid, account_type="Current").one())
    deposit(off, race.account_number, "5000")
    tellers = [client("officer@bank.test", PW) for _ in range(10)]
    forms = [form_token(t, f"/transactions/new?account={race.account_number}") for t in tellers]
    go = threading.Barrier(10)

    def withdraw(i):
        go.wait()
        tellers[i].post(f"/transactions/new?account={race.account_number}",
                        data={"_csrf": forms[i][0], "_idem": idem(forms[i][1]), "txn_type": "Debit", "amount": "1000", "channel": "Cash"})

    ts = [threading.Thread(target=withdraw, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    n_debits = q(lambda: M.Transaction.query.filter_by(account_id=race.id, txn_type="Debit").count())
    check("10 simultaneous ₹1,000 withdrawals on a ₹5,000 balance: exactly 5 succeed, balance ₹0",
          n_debits == 5 and balance(race.id) == Decimal("0.00"), f"{n_debits} debits, balance {balance(race.id)}")
    with app.app_context():
        from bank.ledger import reconciliation
        check("Books still reconcile after the race", all(c["ok"] for c in reconciliation()))

    # ---------------- sensitive data in logs and URLs -----------------------------------
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(self.format(record) + " " + str(getattr(record, "__dict__", {})))

    h = Grab()
    logging.getLogger().addHandler(h)
    app.logger.addHandler(h)
    admin.get("/customers/?q=ABCDE1234F")
    admin.get("/customers/search?q=9876543210")
    t = form_token(app.test_client(), "/login")
    app.test_client().post("/login", data={"_csrf": t[0], "email": "someone@bank.test", "password": "Secret#Guess123"})
    logging.getLogger().removeHandler(h)
    app.logger.removeHandler(h)
    joined = "\n".join(records)
    http = [r for r in records if "'event': 'http'" in r]
    check("Request logging is active (one record per request)", len(http) >= 3, f"{len(http)} request records")
    check("Request logs never contain a searched PAN or mobile, or a typed password",
          http and "ABCDE1234F" not in joined and "9876543210" not in joined and "Secret#Guess123" not in joined,
          f"{len(records)} log records checked")
    raw_db = q(lambda: db.session.execute(db.text("select pan_enc, mobile_enc from customer limit 1")).first())
    check("PAN and mobile are encrypted in the database", raw_db and "ABCDE" not in str(raw_db[0]) and "98765" not in str(raw_db[1]))
    aud = q(lambda: [a.details or "" for a in M.AuditLog.query.filter(M.AuditLog.action.like("%search%")).all()])
    check("Audit log records searches without the search term", aud and not any("ABCDE1234F" in a or "9876543210" in a for a in aud))

    # ---------------- headers and error leakage --------------------------------------------
    hd = admin.get("/").headers
    want = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": None,
            "Cross-Origin-Opener-Policy": None, "Cache-Control": None}
    missing = [k for k, v in want.items() if not hd.get(k) or (v and hd.get(k) != v)]
    check("Security headers present; pages with customer data are not cached", not missing and "no-store" in hd.get("Cache-Control", ""),
          missing or hd.get("Cache-Control"))
    r = admin.get("/loans/999999999")
    check("Unknown record gives a plain 404 with no stack trace or SQL", r.status_code == 404 and "Traceback" not in r.get_data(as_text=True)
          and "sqlalchemy" not in r.get_data(as_text=True).lower())
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nsecurity & isolation: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
