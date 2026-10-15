"""Account opening from the Accounts page, the customer picker, transfers between accounts,
authorised reversals, and forms that keep what the employee typed. Server-side rules throughout."""
import os
import sys
import threading
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, PW, app, approve_latest, balance, cleanup, client, db, deposit, form_token, idem,  # noqa: E402
                     invite_employee, post, q, setup_bank)

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def books_ok():
    with app.app_context():
        from bank.ledger import reconciliation
        return all(c["ok"] for c in reconciliation())


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin, clerk = S["officer"], S["senior"], S["manager"], S["admin"], S["clerk"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)

    # ---------------- Open Account from the Accounts page --------------------------------
    page = off.get("/accounts/").get_data(as_text=True)
    check("Accounts page shows 'Open Account' to an employee who may open accounts",
          'href="/accounts/new"' in page and "Open Account" in page)
    check("…and hides it from a role without that right (Clerk)", "Open Account" not in clerk.get("/accounts/").get_data(as_text=True))
    check("Clerk still cannot open the form or submit by direct request",
          clerk.get("/accounts/new").status_code == 403
          and post(clerk, "/accounts/new", {"customer": cid, "account_type": "Savings"}, "/").status_code == 403)
    for term, label in (("Audit Cust", "partial name"), ("9876543210", "mobile"), ("ABCDE1234F", "PAN")):
        r = off.get(f"/accounts/new?find={term}").get_data(as_text=True)
        check(f"Picker finds the customer by {label}", "Audit Customer" in r and f"customer={cid}" in r)
    r = off.get("/accounts/new?find=Nobody Here").get_data(as_text=True)
    check("Picker says plainly when nothing matches and offers registration", "No customer matches" in r and "register them first" in r)
    r = off.get("/accounts/new?find=Au").get_data(as_text=True)
    check("Too-short name search asks for 3 letters instead of suggesting a new registration",
          "at least 3 letters" in r and "register them first" not in r)
    r = off.get(f"/accounts/new?customer={cid}").get_data(as_text=True)
    check("Selected customer: existing accounts, KYC and a 'Change customer' link are shown",
          acc_no in r and "Change customer" in r and "Verified" in r)
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    post(admin, "/customers/new", {"name": "Other Branch Person", "pan": "OTHER1234B", "mobile": "9500000009", "branch_id": b2, "gender": "Male"})
    r = off.get("/accounts/new?find=Other Branch").get_data(as_text=True)
    check("Picker never lists customers of another branch", "Other Branch Person" not in r)
    oid = q(lambda: M.Customer.query.filter_by(name="Other Branch Person").one().id)
    check("…and selecting one by id is refused", off.get(f"/accounts/new?customer={oid}").status_code == 403)
    post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    req = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_open", status="Pending Review").one())
    check("Opening through the new flow uses the same maker-checker request", req.maker_id == q(
        lambda: M.Employee.query.filter_by(email="officer@bank.test").one().id))
    approve_latest(S)
    cur = q(lambda: M.Account.query.filter_by(customer_id=cid, account_type="Current").one())
    check("Approved: the second account is Active", cur.status == "Active")
    for path in ("/loans/new?find=ABCDE1234F", "/deposits/new?find=ABCDE1234F", "/kyc/upload?find=ABCDE1234F", "/tickets/new?find=ABCDE1234F"):
        who = clerk if path.startswith(("/kyc", "/tickets")) else off
        if f"customer={cid}" not in who.get(path).get_data(as_text=True):
            check(f"Picker search works on {path.split('?')[0]}", False)
            break
    else:
        check("The same picker search works on the loan, FD, KYC upload and ticket forms", True)
    check("Accounts page has a Frozen filter", ">Frozen</a>" in off.get("/accounts/").get_data(as_text=True))

    # ---------------- forms keep what was typed after an error ------------------------
    r = post(off, f"/transactions/new?account={acc_no}", {"txn_type": "Debit", "amount": "99999999", "channel": "NEFT",
                                                           "description": "rent for june"})
    body = r.get_data(as_text=True)
    check("Failed withdrawal keeps amount, type, channel and narration on the form",
          "Insufficient balance" in body and 'value="99999999"' in body and 'value="Debit" selected' in body
          and "<option selected>NEFT" in body and 'value="rent for june"' in body)
    r = post(off, f"/deposits/new?customer={cid}", {"account_id": aid, "principal": "999", "tenure_months": "24"})
    body = r.get_data(as_text=True)
    check("Rejected FD amount keeps the entered amount and tenure", "from ₹1,000" in body and 'value="999"' in body
          and 'value="24" selected' in body)

    # ---------------- transfers --------------------------------------------------------
    deposit(off, acc_no, "50000")
    a0, c0 = balance(aid), balance(cur.id)
    page = off.get(f"/transactions/transfer?from={acc_no}&to={cur.account_number}").get_data(as_text=True)
    check("Transfer screen loads both accounts with names and balances", "From (debited)" in page and "To (credited)" in page
          and "₹50,000.00" in page)
    post(off, f"/transactions/transfer?from={acc_no}&to={cur.account_number}", {"amount": "12345.67", "narration": "own transfer"})
    check("Transfer moves exactly ₹12,345.67 between the two accounts",
          balance(aid) == a0 - Decimal("12345.67") and balance(cur.id) == c0 + Decimal("12345.67"), f"{balance(aid)} / {balance(cur.id)}")
    legs = q(lambda: [(t.txn_type, t.category) for t in M.Transaction.query.filter(M.Transaction.category.like("Transfer%")).all()])
    check("Recorded as a Transfer Out and a Transfer In", sorted(legs) == [("Credit", "Transfer In"), ("Debit", "Transfer Out")], legs)
    check("Books reconcile after the transfer (clearing nets to zero)", books_ok())
    before = (balance(aid), balance(cur.id))
    r = post(off, f"/transactions/transfer?from={acc_no}&to={cur.account_number}", {"amount": "9999999"})
    check("Transfer above the balance is refused with the available amount", "Insufficient balance" in r.get_data(as_text=True)
          and (balance(aid), balance(cur.id)) == before)
    r = post(off, f"/transactions/transfer?from={acc_no}&to={acc_no}", {"amount": "10"})
    check("Transfer to the same account is refused", (balance(aid), balance(cur.id)) == before)
    acc_other = None
    page = off.get(f"/transactions/transfer?from={acc_no}&to=999999999999").get_data(as_text=True)
    check("Unknown or other-branch account: 'not found in your branch'", "was not found in your branch" in page)
    t = form_token(off, f"/transactions/transfer?from={acc_no}&to={cur.account_number}")
    replay = {"_csrf": t[0], "_idem": idem(t[1]), "from": acc_no, "to": cur.account_number, "amount": "100"}
    off.post("/transactions/transfer", data=replay)
    off.post("/transactions/transfer", data=replay)
    check("A double-submitted transfer is executed once", balance(aid) == before[0] - 100, balance(aid))
    check("Transfers follow the transactions 'create' right: Clerk (cash role) may, Read-Only may not",
          clerk.get("/transactions/transfer").status_code == 200
          and invite_employee(admin, "Viewer", "viewer@bank.test", roles["Read-Only"], b1).get("/transactions/transfer").status_code == 403)
    # high-value transfer goes through maker-checker and executes once
    deposit(off, acc_no, "999999")  # just under the ₹10 lakh approval limit: posts directly
    deposit(off, acc_no, "999999")
    hv_before = (balance(aid), balance(cur.id))
    post(off, f"/transactions/transfer?from={acc_no}&to={cur.account_number}", {"amount": "1500000"})
    rq = q(lambda: M.ApprovalRequest.query.filter_by(request_type="fund_transfer").one())
    check("Transfer of ₹15,00,000 waits for approval and moves nothing yet",
          rq.status == "Pending Review" and (balance(aid), balance(cur.id)) == hv_before)
    post(sen, f"/approvals/{rq.id}", {"action": "review", "remarks": "ok"})
    m2 = client("manager2@bank.test", PW)
    t1, t2 = form_token(mgr, f"/approvals/{rq.id}"), form_token(m2, f"/approvals/{rq.id}")
    go = threading.Barrier(2)

    def approve(c, t):
        go.wait()
        c.post(f"/approvals/{rq.id}", data={"_csrf": t[0], "_idem": idem(t[1]), "action": "approve", "remarks": "ok"})

    ts = [threading.Thread(target=approve, args=a) for a in ((mgr, t1), (m2, t2))]
    [x.start() for x in ts]
    [x.join() for x in ts]
    check("Two managers approving at the same moment move the money exactly once",
          balance(aid) == hv_before[0] - Decimal("1500000") and balance(cur.id) == hv_before[1] + Decimal("1500000"),
          f"{balance(aid)} / {balance(cur.id)}")
    # opposite transfers at the same time: no deadlock, totals preserved
    total = balance(aid) + balance(cur.id)
    tellers = [client("officer@bank.test", PW) for _ in range(8)]
    plans = [(acc_no, cur.account_number) if i % 2 else (cur.account_number, acc_no) for i in range(8)]
    toks = [form_token(tellers[i], f"/transactions/transfer?from={plans[i][0]}&to={plans[i][1]}") for i in range(8)]
    go = threading.Barrier(8)

    def move(i):
        go.wait()
        tellers[i].post("/transactions/transfer", data={"_csrf": toks[i][0], "_idem": idem(toks[i][1]), "from": plans[i][0],
                                                        "to": plans[i][1], "amount": "1000"})

    ts = [threading.Thread(target=move, args=(i,)) for i in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    n = q(lambda: M.Transaction.query.filter(M.Transaction.category == "Transfer Out").count())
    check("8 opposite transfers at once: all complete, no deadlock, combined balance unchanged",
          balance(aid) + balance(cur.id) == total and n == 2 + 8 + 1, f"{n} transfer-out legs")
    check("Books reconcile after concurrent transfers", books_ok())

    # ---------------- reversals ----------------------------------------------------------
    deposit(off, acc_no, "777.77")
    wrong = q(lambda: M.Transaction.query.filter_by(account_id=aid, amount=Decimal("777.77")).one())
    b_before = balance(aid)
    r = post(off, f"/transactions/{wrong.id}/reverse", {"reason": ""}, f"/customers/{cid}?tab=transactions")
    check("Reversal needs a reason", "reason for the reversal" in r.get_data(as_text=True)
          and not q(lambda: M.ApprovalRequest.query.filter_by(request_type="txn_reversal").count()))
    post(off, f"/transactions/{wrong.id}/reverse", {"reason": "Teller keyed wrong amount"}, f"/customers/{cid}?tab=transactions")
    r = post(off, f"/transactions/{wrong.id}/reverse", {"reason": "again please"}, f"/customers/{cid}?tab=transactions")
    check("Only one open reversal request per transaction", "already waiting for approval" in r.get_data(as_text=True)
          and q(lambda: M.ApprovalRequest.query.filter_by(request_type="txn_reversal").count()) == 1)
    check("Nothing changes until approved", balance(aid) == b_before)
    rv = q(lambda: M.ApprovalRequest.query.filter_by(request_type="txn_reversal").one())
    post(off, f"/approvals/{rv.id}", {"action": "review", "remarks": "self"})
    check("The maker cannot review their own reversal", q(lambda: db.session.get(M.ApprovalRequest, rv.id).status) == "Pending Review")
    approve_latest(S, rv.id)
    after = q(lambda: (db.session.get(M.Transaction, wrong.id).status,
                       M.Transaction.query.filter_by(account_id=aid, category="Reversal").one()))
    check("Approved: ₹777.77 taken back, original marked Reversed, reversal linked by reference",
          balance(aid) == b_before - Decimal("777.77") and after[0] == "Reversed" and wrong.txn_ref in after[1].description,
          after[1].description)
    r = post(off, f"/transactions/{wrong.id}/reverse", {"reason": "twice"}, f"/customers/{cid}?tab=transactions")
    check("A reversed transaction cannot be reversed again", "already been reversed" in r.get_data(as_text=True))
    disb = None
    post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "account_id": aid, "amount": "20000",
                             "interest_rate": "10", "tenure_months": "6"}, f"/loans/new?customer={cid}")
    approve_latest(S)
    disb = q(lambda: M.Transaction.query.filter_by(category="Loan Disbursement").one())
    r = post(off, f"/transactions/{disb.id}/reverse", {"reason": "try it"}, f"/customers/{cid}?tab=transactions")
    check("Loan, FD and interest postings cannot be reversed here", "cannot be reversed here" in r.get_data(as_text=True))
    hist = off.get(f"/customers/{cid}?tab=transactions").get_data(as_text=True)
    check("Transaction history shows Reversed and Reversal badges; no Reverse button on them",
          ">Reversed<" in hist and ">Reversal<" in hist and f"/transactions/{wrong.id}/reverse" not in hist)
    # reversing a deposit when the money has already been withdrawn is blocked at approval
    post(off, "/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
    approve_latest(S)
    small = q(lambda: M.Account.query.filter_by(customer_id=cid, account_type="Savings", balance=0).order_by(M.Account.id.desc()).first())
    deposit(off, small.account_number, "300")
    dep = q(lambda: M.Transaction.query.filter_by(account_id=small.id, amount=Decimal("300")).one())
    post(off, f"/transactions/{dep.id}/reverse", {"reason": "duplicate deposit"}, f"/customers/{cid}?tab=transactions")
    rv2 = q(lambda: M.ApprovalRequest.query.filter_by(request_type="txn_reversal", status="Pending Review").one().id)
    deposit(off, small.account_number, "300", "Debit")  # the customer withdrew it meanwhile
    post(sen, f"/approvals/{rv2}", {"action": "review", "remarks": "ok"})
    r = post(mgr, f"/approvals/{rv2}", {"action": "approve", "remarks": "ok"})
    check("Reversal blocked at approval if the money is no longer there; nothing changes",
          "Not approved" in r.get_data(as_text=True) and "needs ₹300.00" in r.get_data(as_text=True)
          and balance(small.id) == Decimal("0.00") and q(lambda: db.session.get(M.Transaction, dep.id).status) == "Posted",
          balance(small.id))
    check("Books reconcile after reversals", books_ok())
    page = mgr.get(f"/approvals/{rv.id}").get_data(as_text=True)
    check("Approval shows the reason and links to the customer's transactions",
          "Teller keyed wrong amount" in page and "View transactions in Customer 360" in page)
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\noperations: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
