"""Reproduce suspected defects against a real PostgreSQL database. Prints CONFIRMED (bug present) or OK."""
import os
import sys
import threading
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (PW, M, app, approve_latest, balance, cleanup, client, db, deposit, form_token, post,
                           q, setup_bank)

results = []


def report(code, title, bug, detail):
    results.append((code, "CONFIRMED" if bug else "OK", title, detail))
    print(f"[{'CONFIRMED' if bug else 'OK':9}] {code} {title} :: {detail}")


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, mgr2, clerk, admin = (S[k] for k in ("officer", "senior", "manager", "manager2", "clerk", "admin"))

    # --- T1/T2 amount validation -------------------------------------------------
    deposit(off, acc_no, 1000)
    def reset_balance(v):
        with app.app_context():
            db.session.get(M.Account, aid).balance = v
            db.session.commit()
    for bad in ["nan", "inf", "-500", "0", "1e15"]:
        reset_balance(1000.0)
        before = balance(aid)
        r = deposit(off, acc_no, bad)
        after = balance(aid)
        accepted = after != before or after != after  # NaN != NaN
        report("T1", f"Deposit amount '{bad}'", accepted, f"balance {before} -> {after}")
        if bad == "nan" and accepted:
            dash = admin.get("/").status_code
            page = off.get(f"/transactions/new?account={acc_no}").status_code
            report("T1b", "After a NaN deposit: dashboard / account posting page", True, f"HTTP {dash} / HTTP {page}")
        # (history is append-only for the app role, so nothing could be deleted here anyway)
    reset_balance(1000.0)

    # --- T3 duplicate submission (double click / resubmit same form) --------------
    t, html = form_token(off, f"/transactions/new?account={acc_no}")
    import re
    idem = re.search(r'name="_idem" value="([^"]+)"', html)
    data = {"_csrf": t, "txn_type": "Credit", "amount": "777", "channel": "Cash"}
    if idem:
        data["_idem"] = idem.group(1)
    n0 = q(lambda: M.Transaction.query.count())
    off.post(f"/transactions/new?account={acc_no}", data=data)
    off.post(f"/transactions/new?account={acc_no}", data=data)
    n1 = q(lambda: M.Transaction.query.count())
    report("T3", "Same deposit form submitted twice", n1 - n0 == 2, f"{n1 - n0} transactions created")

    # --- T4 concurrent deposits (lost update) ------------------------------------
    start_bal = balance(aid)
    clients = [client("officer@bank.test", PW) for _ in range(12)]
    def worker(c):
        deposit(c, acc_no, 1000)
    th = [threading.Thread(target=worker, args=(c,)) for c in clients]
    [x.start() for x in th]; [x.join() for x in th]
    got = balance(aid) - start_bal
    ntx = q(lambda: M.Transaction.query.filter_by(account_id=aid, amount=1000).count())
    report("T4", "12 simultaneous deposits of 1,000", abs(got - 12000) > 0.001,
           f"{ntx} deposit rows of 1,000 on the account, but balance rose by {got:,.2f} (expected 12,000)")

    # --- T5 concurrent final approval (double disbursal) --------------------------
    post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "account_id": aid, "amount": "100000",
                             "interest_rate": "12", "tenure_months": "12"}, f"/loans/new?customer={cid}")
    rid = q(lambda: M.ApprovalRequest.query.filter_by(status="Pending Review").order_by(M.ApprovalRequest.id.desc()).first().id)
    post(sen, f"/approvals/{rid}", {"action": "review", "remarks": "ok"})
    def tok_pair(c):
        t, html = form_token(c, f"/approvals/{rid}")
        return t, re.search(r'name="_idem" value="([^"]+)"', html).group(1)
    tokens = [tok_pair(c) for c in (mgr, mgr2)]
    def approve(c, t):
        c.post(f"/approvals/{rid}", data={"_csrf": t[0], "_idem": t[1], "action": "approve", "remarks": "go"})
    th = [threading.Thread(target=approve, args=(c, t)) for c, t in zip((mgr, mgr2), tokens)]
    [x.start() for x in th]; [x.join() for x in th]
    lid = q(lambda: M.Loan.query.filter_by(loan_type="personal").first().id)
    disb = q(lambda: M.Transaction.query.filter_by(category="Loan Disbursement").count())
    report("T5", "Two managers approve the same loan at the same moment", disb > 1, f"{disb} disbursement transaction(s)")

    # --- T6/T7 repayment maths ---------------------------------------------------
    loan = q(lambda: db.session.get(M.Loan, lid))
    out0 = loan.outstanding
    post(sen, f"/loans/{lid}/repay", {"amount": "1000", "mode": "Cash"}, f"/loans/{lid}")
    post(sen, f"/loans/{lid}/repay", {"amount": "1000", "mode": "Cash"}, f"/loans/{lid}")
    reps = q(lambda: [(r.interest_component, r.principal_component) for r in M.LoanRepayment.query.filter_by(loan_id=lid).all()])
    report("T7", "Two ₹1,000 part-payments on the day of disbursal", sum(i for i, _ in reps) > 1,
           f"interest charged {[i for i, _ in reps]} (a full month each; ~0 days have elapsed)")
    out1 = q(lambda: db.session.get(M.Loan, lid).outstanding)
    r = post(sen, f"/loans/{lid}/repay", {"amount": str(out1 * 10), "mode": "Cash"}, f"/loans/{lid}")
    last = q(lambda: M.LoanRepayment.query.filter_by(loan_id=lid).order_by(M.LoanRepayment.id.desc()).first())
    unaccounted = last.amount - last.principal_component - last.interest_component
    report("T6", f"Repayment of 10× outstanding ({out1 * 10:,.0f})", unaccounted > 1,
           f"recorded ₹{last.amount:,.0f}, applied ₹{last.principal_component + last.interest_component:,.0f}, ₹{unaccounted:,.0f} unaccounted")

    # --- T8 account closure with money in it -------------------------------------
    bal = balance(aid)
    r = post(admin, f"/accounts/{aid}/close", {"reason": "test"}, f"/customers/{cid}?tab=accounts")
    pending = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_close", status="Pending Review").first())
    if pending:
        approve_latest(S, pending.id)
    acc = q(lambda: db.session.get(M.Account, aid))
    report("T8", f"Close account holding ₹{bal:,.0f}", acc.status == "Closed" and acc.balance > 0,
           f"status {acc.status}, balance left {acc.balance:,.2f}")

    # second account for further money tests
    post(admin, "/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
    approve_latest(S)
    aid2, acc2 = q(lambda: (lambda a: (a.id, a.account_number))(M.Account.query.filter_by(status="Active").order_by(M.Account.id.desc()).first()))
    deposit(off, acc2, 100000)

    # --- T9 FD maturity payout ----------------------------------------------------
    post(off, "/deposits/new", {"customer": cid, "account_id": aid2, "principal": "50000", "tenure_months": "12"},
         f"/deposits/new?customer={cid}")
    approve_latest(S)
    with app.app_context():
        fd = M.FixedDeposit.query.first()
        fd.maturity_date = date.today() - timedelta(days=1)
        db.session.commit()
        before = db.session.get(M.Account, aid2).balance
        from bank.services import scan_scheduled_alerts
        scan_scheduled_alerts(force=True)
        after = db.session.get(M.Account, aid2).balance
        fd = M.FixedDeposit.query.first()
        report("T9", "Matured FD pays out", after <= before, f"FD status {fd.status}; account {before:,.0f} -> {after:,.0f} (maturity value {fd.maturity_amount:,.0f})")

    # --- T10 timezone: 00:15 local time today ------------------------------------
    with app.app_context():
        local_midnight = datetime.combine(date.today(), datetime.min.time())
        ts_utc = local_midnight + timedelta(minutes=15) - timedelta(minutes=330)
        a = db.session.get(M.Account, aid2)
        db.session.add(M.Transaction(txn_ref="TXNTZCHECK0001", account_id=a.id, customer_id=a.customer_id,
                                     branch_id=a.branch_id, txn_type="Credit", channel="Cash", amount=1,
                                     balance_after=a.balance, timestamp=ts_utc))
        db.session.commit()
    page = admin.get("/transactions/?range=today").get_data(as_text=True)
    report("T10", "Deposit made at 00:15 IST appears under 'Today'", "TXNTZCHECK0001" not in page,
           "missing from Today" if "TXNTZCHECK0001" not in page else "shown")

    # --- T11 account enumeration through login messages --------------------------
    def fail6(email):
        c = app.test_client()
        msg = ""
        for _ in range(6):
            t = form_token(c, "/login")[0]
            msg = c.post("/login", data={"_csrf": t, "email": email, "password": "wrong"}).get_data(as_text=True)
        return "temporarily locked" in msg.lower()
    real, fake = fail6("clerk@bank.test"), fail6("nobody@bank.test")
    report("T11", "Login reveals which emails are real employees", real != fake,
           f"real email -> {'locked message' if real else 'generic'}, unknown email -> {'locked message' if fake else 'generic'}")

    # --- T12 server-side validation ----------------------------------------------
    post(admin, f"/customers/{cid}/edit", {"mobile": "not-a-phone", "email": "x", "address": "a", "city": "b",
                                           "occupation": "c", "risk_category": "Low"})
    m = q(lambda: db.session.get(M.Customer, cid).mobile)
    report("T12", "Edit customer mobile to 'not-a-phone'", m == "not-a-phone", f"stored mobile = {m!r}")
    n0 = q(lambda: M.Loan.query.count())
    rr = post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "amount": "50000", "interest_rate": "500",
                                  "tenure_months": "9999"}, f"/loans/new?customer={cid}")
    made = q(lambda: M.Loan.query.count()) > n0
    report("T13a", "Loan at 500% interest for 9,999 months", made or rr.status_code >= 500,
           f"HTTP {rr.status_code}, {'loan created' if made else 'no loan created'}")
    n0 = q(lambda: M.Loan.query.count())
    post(off, "/loans/new", {"customer": cid, "loan_type": "gold", "amount": "300000", "interest_rate": "9",
                             "tenure_months": "12", "gold_weight_grams": "10", "gold_valuation": "60000"}, f"/loans/new?customer={cid}")
    report("T13b", "Gold loan of ₹3,00,000 against gold valued ₹60,000 (LTV 500%)", q(lambda: M.Loan.query.count()) > n0,
           "accepted" if q(lambda: M.Loan.query.count()) > n0 else "rejected")

    # --- T14 rejected account opening shows as Closed ----------------------------
    post(admin, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    rid = q(lambda: M.ApprovalRequest.query.filter_by(status="Pending Review").order_by(M.ApprovalRequest.id.desc()).first().id)
    post(sen, f"/approvals/{rid}", {"action": "reject", "remarks": "docs missing"})
    st = q(lambda: M.Account.query.filter_by(account_type="Current").first().status)
    report("T14", "Rejected account-opening request", st == "Closed", f"account status = {st} (counted as a closed account)")

    # --- T15 backend permission checks (direct POSTs) ----------------------------
    def code(c, url, data=None):
        t = form_token(c, "/")[0]
        return c.post(url, data=dict(data or {}, _csrf=t)).status_code
    checks = [
        ("clerk", clerk, "/loans/new", {"customer": cid}),
        ("clerk", clerk, f"/approvals/{rid}", {"action": "approve"}),
        ("clerk", clerk, "/users/new", {"email": "x@y.z"}),
        ("clerk", clerk, "/settings/", {}),
        ("clerk", clerk, f"/loans/{lid}/repay", {"amount": "1"}),
        ("officer", off, "/users/roles/2", {}),
        ("officer", off, f"/accounts/{aid2}/close", {}),
    ]
    leaks = [(who, url, c) for who, cl, url, d in checks if (c := code(cl, url, d)) not in (302, 400, 403, 404)]
    report("T15", "Forbidden POSTs sent directly to the server", bool(leaks), f"{len(checks)} attempts, leaks: {leaks or 'none'}")
    post(admin, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    rid2 = q(lambda: M.ApprovalRequest.query.filter_by(status="Pending Review").order_by(M.ApprovalRequest.id.desc()).first().id)
    t = form_token(admin, f"/approvals/{rid2}")[0]
    sc = admin.post(f"/approvals/{rid2}", data={"_csrf": t, "action": "review", "remarks": "self"}).status_code
    report("T15b", "Maker reviews own request (Super Admin)", sc not in (403,) and q(lambda: db.session.get(M.ApprovalRequest, rid2).status) != "Pending Review", f"HTTP {sc}")

    # cross-branch IDOR
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    post(admin, "/customers/new", {"name": "Other Branch Cust", "pan": "ZZZZZ9999Z", "mobile": "9123456780", "branch_id": b2})
    ocid = q(lambda: M.Customer.query.filter_by(branch_id=b2).one().id)
    sc = sen.get(f"/customers/{ocid}").status_code
    report("T15c", "Branch-1 officer opens a Branch-2 customer by URL", sc == 200, f"HTTP {sc}")

    # --- T16 KYC document with no file can be verified ---------------------------
    t, html = form_token(admin, f"/kyc/upload?customer={cid}")
    idem = re.search(r'name="_idem" value="([^"]+)"', html).group(1)
    admin.post("/kyc/upload", data={"_csrf": t, "_idem": idem, "customer": cid, "doc_type": "Passport", "doc_number": "P123"})
    d = q(lambda: M.Document.query.filter_by(doc_type="Passport").first())
    report("T16", "KYC document uploaded with no file attached", d is not None, "accepted for verification" if d else "rejected")

    # --- T17 large transaction without maker-checker -----------------------------
    n0 = q(lambda: M.Transaction.query.count())
    deposit(off, acc2, 9000000)
    deposit(off, acc2, 8000000, "Debit")
    report("T17", "₹80 lakh withdrawal by an Officer", q(lambda: M.Transaction.query.count()) - n0 == 2,
           "posted immediately; only an alert is raised")

    # --- T18 MFA secret at rest (enrol through the real screen) -----------------
    import pyotp
    page = clerk.get("/settings/mfa").get_data(as_text=True)
    secret = re.search(r'<p class="mono">([A-Z2-7]{16,})</p>', page).group(1)
    t = form_token(clerk, "/settings/mfa")[0]
    clerk.post("/settings/mfa/enable", data={"_csrf": t, "code": pyotp.TOTP(secret).now()})
    with app.app_context():
        raw = db.session.execute(db.text("select mfa_secret from employee where email='clerk@bank.test'")).scalar()
    report("T18", "MFA secret stored readable in the database", raw == secret, f"column starts {str(raw)[:16]}…")

    # --- T19 high-value transaction goes through maker-checker and then posts ------
    before = balance(aid2)
    n_apr = q(lambda: M.ApprovalRequest.query.filter_by(request_type="large_txn").count())
    if n_apr:
        approve_latest(S, q(lambda: M.ApprovalRequest.query.filter_by(request_type="large_txn").order_by(M.ApprovalRequest.id).first().id))
    after = balance(aid2)
    report("T19", "High-value deposit after checker + manager approval", n_apr and after - before != 9000000,
           f"{n_apr} approval request(s); balance {before:,.2f} -> {after:,.2f}")

    # --- T20 exact payoff closes the loan with nothing unaccounted ------------------
    post(off, "/loans/new", {"customer": cid, "loan_type": "vehicle", "account_id": aid2, "amount": "120000",
                             "interest_rate": "10", "tenure_months": "24"}, f"/loans/new?customer={cid}")
    approve_latest(S)
    vl = q(lambda: M.Loan.query.filter_by(loan_type="vehicle").first())
    page = sen.get(f"/loans/{vl.id}").get_data(as_text=True)
    m = re.search(r"Full payoff today: <strong>₹([\d,\.]+)", page)
    payoff = float(m.group(1).replace(",", "")) if m else vl.outstanding
    post(sen, f"/loans/{vl.id}/repay", {"amount": str(payoff), "mode": "Cash"}, f"/loans/{vl.id}")
    vl = q(lambda: M.Loan.query.filter_by(loan_type="vehicle").first())
    report("T20", f"Pay the displayed payoff ₹{payoff:,.2f}", not (vl.status == "Closed" and vl.outstanding == 0),
           f"loan {vl.status}, outstanding {vl.outstanding}")
finally:
    cleanup()

print("\nconfirmed:", sum(1 for r in results if r[1] == "CONFIRMED"), "/", len(results))
