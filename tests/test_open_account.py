"""Account opening end to end: both entry points (Accounts → Open Account, Customer 360), the
customer search, permissions, validation, eligibility (KYC, customer status), duplicate submissions
and duplicate pending requests, concurrency, and the maker-checker approval outcomes."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, PW, app, approve_latest, cleanup, client, db, form_token, idem, invite_employee,  # noqa: E402
                     post, q, setup_bank, upload)

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def accounts(cid, **kw):
    return q(lambda: M.Account.query.filter_by(customer_id=cid, **kw).count())


def set_customer(cid, **kw):
    with app.app_context():
        c = db.session.get(M.Customer, cid)
        for k, v in kw.items():
            setattr(c, k, v)
        db.session.commit()


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin, clerk = S["officer"], S["senior"], S["manager"], S["admin"], S["clerk"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)
    code = q(lambda: M.Customer.query.get(cid).customer_code)

    # ---------------- entry points and permissions ----------------------------------
    for who, c in (("Officer", off), ("Senior Officer", sen), ("Branch Manager", mgr)):
        check(f"{who}: 'Open Account' on the Accounts page", 'href="/accounts/new"' in c.get("/accounts/").get_data(as_text=True))
    viewer = invite_employee(admin, "Viewer", "viewer@bank.test", roles["Read-Only"], b1)
    accountant = invite_employee(admin, "Accountant", "acct@bank.test", roles["Accountant"], b1)
    for who, c in (("Clerk", clerk), ("Read-Only", viewer), ("Accountant", accountant)):
        page = c.get("/accounts/").get_data(as_text=True)
        t, html = form_token(c, "/accounts/")
        sc = c.post("/accounts/new", data={"_csrf": t, "_idem": idem(html), "customer": cid, "account_type": "Savings"}).status_code
        check(f"{who}: no button, form 403, direct POST 403, nothing created",
              "Open Account" not in page and c.get("/accounts/new").status_code == 403 and sc == 403
              and accounts(cid, account_type="Savings") == 1, sc)
    c360 = off.get(f"/customers/{cid}").get_data(as_text=True)
    check("Customer 360 still offers 'Open new account' with the customer pre-selected",
          "Open new account" in c360 and f"/accounts/new?customer={cid}" in c360)

    # ---------------- customer search and selection --------------------------------------
    for term in ("Audit Cust", "9876543210", "ABCDE1234F", code, acc_no):
        r = off.get(f"/accounts/new?find={term}").get_data(as_text=True)
        if f"customer={cid}" not in r:
            check(f"Search finds the customer by '{term}'", False)
            break
    else:
        check("Search finds the customer by name, mobile, PAN, customer ID and account number", True)
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    post(admin, "/customers/new", {"name": "Branch Two Person", "pan": "BRTWO9876P", "mobile": "9400000004", "branch_id": b2, "gender": "Male"})
    other = q(lambda: M.Customer.query.filter_by(name="Branch Two Person").one().id)
    check("Another branch's customer is not listed and cannot be selected by id",
          "Branch Two Person" not in off.get("/accounts/new?find=Branch Two").get_data(as_text=True)
          and off.get(f"/accounts/new?customer={other}").status_code == 403)
    sel = off.get(f"/accounts/new?customer={cid}").get_data(as_text=True)
    check("Selected customer: details, existing accounts, rules and a review step are shown",
          code in sel and acc_no in sel and "Change customer" in sel and 'data-confirm-title="Review account request"' in sel
          and "Submit for approval" in sel)

    # ---------------- validation -------------------------------------------------------
    r = post(off, "/accounts/new", {"customer": cid, "account_type": "Fixed"}, f"/accounts/new?customer={cid}")
    body = r.get_data(as_text=True)
    check("Unknown account type: clear message, form shown again, nothing created",
          "Choose an account type: Savings or Current" in body and "Submit for approval" in body and accounts(cid) == 1)
    t, html = form_token(off, "/accounts/new")
    r = off.post("/accounts/new", data={"_csrf": t, "_idem": idem(html), "account_type": "Savings"}, follow_redirects=True)
    check("Submission without a customer creates nothing and shows the search", accounts(cid) == 1 and "Find the customer" in r.get_data(as_text=True))

    # ---------------- eligibility: KYC and customer status ------------------------------
    set_customer(cid, kyc_status="Pending")
    r = post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    check("KYC not verified: refused with the reason", "Verify PAN and Aadhaar" in r.get_data(as_text=True)
          and accounts(cid, account_type="Current") == 0)
    set_customer(cid, kyc_status="Verified", status="Inactive")
    r = post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    check("Inactive customer record: refused with the reason", "only for active customers" in r.get_data(as_text=True)
          and accounts(cid, account_type="Current") == 0)
    check("The form explains it before submission", "only for active customers" in off.get(f"/accounts/new?customer={cid}").get_data(as_text=True))
    set_customer(cid, status="Active")

    # ---------------- duplicate submissions and duplicate pending requests ------------
    t, html = form_token(off, f"/accounts/new?customer={cid}")
    data = {"_csrf": t, "_idem": idem(html), "customer": cid, "account_type": "Current"}
    off.post("/accounts/new", data=data)
    r = off.post("/accounts/new", data=data, follow_redirects=True)
    check("Same form submitted twice (double click / resubmit): one request", accounts(cid, account_type="Current") == 1
          and "already submitted" in r.get_data(as_text=True))
    r = post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    req1 = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_open", status="Pending Review").one())
    check("A second Current request while the first is pending is refused, naming the request",
          req1.request_number in r.get_data(as_text=True) and accounts(cid, account_type="Current") == 1)
    check("The form shows the waiting request up front", req1.request_number in off.get(f"/accounts/new?customer={cid}").get_data(as_text=True))
    tellers = [client("officer@bank.test", PW) for _ in range(4)] + [client("senior@bank.test", PW) for _ in range(2)]
    toks = [form_token(t, f"/accounts/new?customer={cid}") for t in tellers]
    go = threading.Barrier(len(tellers))

    def submit(i):
        go.wait()
        tellers[i].post("/accounts/new", data={"_csrf": toks[i][0], "_idem": idem(toks[i][1]), "customer": cid, "account_type": "Savings"})

    # Widen the window between the eligibility check and the insert, so the race is real rather
    # than left to chance: without the customer row lock, several requests get through here.
    import time

    import bank.views.accounts as acc_view
    original_check = acc_view.account_open_problem

    def slow_check(*a, **kw):
        result = original_check(*a, **kw)
        time.sleep(0.4)
        return result

    acc_view.account_open_problem = slow_check
    try:
        ts = [threading.Thread(target=submit, args=(i,)) for i in range(len(tellers))]
        [x.start() for x in ts]
        [x.join() for x in ts]
    finally:
        acc_view.account_open_problem = original_check
    check("6 employees submitting a Savings request at the same moment: exactly one is raised",
          accounts(cid, account_type="Savings", status="Pending") == 1, accounts(cid, account_type="Savings", status="Pending"))

    # ---------------- approval outcomes --------------------------------------------------
    post(off, f"/approvals/{req1.id}", {"action": "review", "remarks": "self"})
    check("The maker cannot review their own request", q(lambda: db.session.get(M.ApprovalRequest, req1.id).status) == "Pending Review")
    post(sen, f"/approvals/{req1.id}", {"action": "review", "remarks": "ok"})
    set_customer(cid, kyc_status="Expired")
    r = post(mgr, f"/approvals/{req1.id}", {"action": "approve", "remarks": "ok"})
    check("KYC lapsed while waiting: approval refused with the reason, account still Pending",
          "Not approved" in r.get_data(as_text=True) and accounts(cid, account_type="Current", status="Pending") == 1)
    set_customer(cid, kyc_status="Verified")
    post(mgr, f"/approvals/{req1.id}", {"action": "approve", "remarks": "ok"})
    check("Checker + manager approval: the Current account becomes Active", accounts(cid, account_type="Current", status="Active") == 1)
    sav = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_open", status="Pending Review").one().id)
    post(S["manager2"], f"/approvals/{sav}", {"action": "reject", "remarks": "Customer withdrew the request"})  # never the maker
    check("Rejected request: account marked Rejected", accounts(cid, account_type="Savings", status="Rejected") == 1)
    post(off, "/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
    check("After a rejection a new request can be raised", accounts(cid, account_type="Savings", status="Pending") == 1)

    # ---------------- both entry points use the same request and audit trail ------------
    via_360 = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_open").order_by(M.ApprovalRequest.id.desc()).first())
    aud = q(lambda: M.AuditLog.query.filter_by(action="Approval request created", reference=via_360.request_number).count())
    act = q(lambda: M.Activity.query.filter_by(customer_id=cid, reference=via_360.request_number).count())
    check("Every request is audit-logged and shown in the customer's timeline", aud == 1 and act >= 1, (aud, act))
    post(admin, "/customers/new", {"name": "Fresh Customer", "pan": "FRESH1234F", "mobile": "9300000003",
                                   "branch_id": b1, "gender": "Female"})
    fresh = q(lambda: M.Customer.query.filter_by(name="Fresh Customer").one().id)
    for dt, no in (("PAN Card", "FRESH1234F"), ("Aadhaar", "999911112222")):
        upload(admin, fresh, dt, no)
    for d in q(lambda: [d.id for d in M.Document.query.filter_by(customer_id=fresh)]):
        post(sen, f"/kyc/doc/{d}/verify", {"decision": "approve"}, f"/kyc/doc/{d}")
    r = post(off, "/accounts/new", {"customer": fresh, "account_type": "Savings"}, f"/accounts/new?customer={fresh}")
    body = r.get_data(as_text=True)
    check("Success message states type, account, customer and that it is pending approval",
          "Savings account" in body and "for Fresh Customer is pending approval" in body and "checker reviews it" in body)
    approve_latest(S)
    check("Accounts page lists the new account once approved", "Fresh Customer" in off.get("/accounts/?status=Active").get_data(as_text=True))
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nopen account: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
