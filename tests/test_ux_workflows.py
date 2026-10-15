"""Employee workflow improvements: KYC gate on account opening, safe KYC decisions, work queues,
direct search hits, and plain-language limits. Each check is enforced server-side."""
import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import M, app, approve_latest, cleanup, db, deposit, post, q, setup_bank, upload  # noqa: E402

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin = S["officer"], S["senior"], S["manager"], S["admin"]
    b1 = q(lambda: M.Branch.query.first().id)

    # ---------------- account opening needs verified KYC --------------------------------
    post(admin, "/customers/new", {"name": "Walkin Pending", "pan": "WALKP1234Q", "mobile": "9811112222",
                                   "branch_id": b1, "gender": "Male"})
    pid = q(lambda: M.Customer.query.filter_by(name="Walkin Pending").one().id)
    page = off.get(f"/accounts/new?customer={pid}").get_data(as_text=True)
    check("Account form explains that KYC must be verified first", "can be opened only after PAN and Aadhaar" in page
          and "Submit for approval" not in page)
    r = post(off, "/accounts/new", {"customer": pid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
    n = q(lambda: M.Account.query.filter_by(customer_id=pid).count())
    check("Server refuses an account request without verified KYC", n == 0 and "Verify PAN and Aadhaar" in r.get_data(as_text=True), n)
    # KYC lapses while a request waits → the approval is blocked
    post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    rid = q(lambda: M.ApprovalRequest.query.filter_by(request_type="account_open", status="Pending Review").one().id)
    with app.app_context():
        db.session.get(M.Customer, cid).kyc_status = "Expired"
        db.session.commit()
    post(sen, f"/approvals/{rid}", {"action": "review", "remarks": "ok"})
    post(mgr, f"/approvals/{rid}", {"action": "approve", "remarks": "ok"})
    st = q(lambda: (db.session.get(M.ApprovalRequest, rid).status,
                    M.Account.query.filter_by(customer_id=cid, account_type="Current").one().status))
    check("Approval refused if KYC lapsed while the request waited", st == ("Reviewed", "Pending"), st)
    with app.app_context():
        db.session.get(M.Customer, cid).kyc_status = "Verified"
        db.session.commit()

    # ---------------- KYC decisions --------------------------------------------------------
    upload(admin, pid, "PAN Card", "WALKP1234Q")
    upload(admin, pid, "Aadhaar", "999988887777")
    d1, d2 = q(lambda: [d.id for d in M.Document.query.filter_by(customer_id=pid).order_by(M.Document.id)])
    r = post(sen, f"/kyc/doc/{d1}/verify", {"decision": "reject", "remarks": ""}, f"/kyc/doc/{d1}")
    check("Rejecting a KYC document requires a reason", q(lambda: db.session.get(M.Document, d1).status) == "Pending"
          and "reason for rejecting" in r.get_data(as_text=True))
    r = post(sen, f"/kyc/doc/{d1}/verify", {"decision": "approve", "next": "https://evil.example/phish"}, f"/kyc/doc/{d1}",
             follow=False)
    loc = r.headers.get("Location", "")
    check("A 'next' value in the form cannot redirect off-site", "evil" not in loc and loc.startswith("/"), loc)
    check("After verifying, the employee goes to the next pending document", loc.endswith(f"/kyc/doc/{d2}"), loc)
    r = post(sen, f"/kyc/doc/{d1}/verify", {"decision": "reject", "remarks": "changed my mind"}, f"/kyc/doc/{d1}")
    check("A decided document cannot be decided again", q(lambda: db.session.get(M.Document, d1).status) == "Verified"
          and "already verified" in r.get_data(as_text=True))
    page = sen.get(f"/kyc/doc/{d2}").get_data(as_text=True)
    check("Verify and Reject each name the document and customer in their confirmation",
          'data-confirm="Mark Aadhaar of Walkin Pending as verified?' in page and 'data-confirm="Reject Aadhaar of Walkin Pending?' in page)

    # ---------------- work queues and counts ---------------------------------------------
    deposit(off, acc_no, "100000")
    post(off, "/deposits/new", {"customer": cid, "account_id": aid, "principal": "20250.50", "tenure_months": "12"},
         f"/deposits/new?customer={cid}")
    fd = q(lambda: M.FixedDeposit.query.order_by(M.FixedDeposit.id.desc()).first())
    check("FD accepts any amount from ₹1,000 in rupees and paise (₹20,250.50)", fd and fd.principal == Decimal("20250.50"),
          fd.principal if fd else None)
    post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "account_id": aid, "amount": "50000",
                             "interest_rate": "12", "tenure_months": "12"}, f"/loans/new?customer={cid}")
    nav = sen.get("/customers/").get_data(as_text=True)
    m = re.search(r'Approvals</span><span class="count"[^>]*>(\d+)<', nav)
    waiting = q(lambda: M.ApprovalRequest.query.filter_by(status="Pending Review").count())
    check("Menu shows how many approvals wait for this employee", m and int(m.group(1)) == waiting, f"{m.group(1) if m else None} / {waiting}")
    m = re.search(r'KYC &amp; Documents</span><span class="count"[^>]*>(\d+)<', nav)
    check("Menu shows KYC documents waiting for verification", m and int(m.group(1)) == 1, m.group(1) if m else None)
    check("Maker sees no approval count for their own requests",
          not re.search(r'Approvals</span><span class="count"', off.get("/customers/").get_data(as_text=True)))
    lst = sen.get("/approvals/").get_data(as_text=True)
    check("Approvals open on 'Awaiting me' for a checker", 'class="on">Awaiting me' in lst and "FD of ₹20,250.50" in lst)
    first, second = q(lambda: [r.id for r in M.ApprovalRequest.query.filter_by(status="Pending Review").order_by(M.ApprovalRequest.created_at)])
    r = post(sen, f"/approvals/{first}", {"action": "review", "remarks": "ok"}, follow=False)
    check("After a decision the checker goes to the next request", r.headers["Location"].endswith(f"/approvals/{second}"), r.headers["Location"])
    r = post(sen, f"/approvals/{second}", {"action": "review", "remarks": "ok"}, follow=True)
    check("When the queue is empty the checker is told so", "Your approval queue is clear" in r.get_data(as_text=True))
    det = mgr.get(f"/approvals/{first}").get_data(as_text=True)
    check("Approve dialog names the request and its summary", re.search(r'data-confirm="Approve APR\d+\? FD of ₹20,250.50', det) is not None)
    r = post(mgr, f"/approvals/{first}", {"action": "reject", "remarks": ""})
    check("Rejection still needs remarks on the server", q(lambda: db.session.get(M.ApprovalRequest, first).status) == "Reviewed")

    # ---------------- top search goes straight to a single hit ---------------------------
    r = off.get(f"/customers/search?q={acc_no}", follow_redirects=False)
    check("Exact account number opens Customer 360 on the accounts tab",
          r.status_code == 302 and r.headers["Location"].endswith(f"/customers/{cid}?tab=accounts"), r.headers.get("Location"))
    approve_latest(S, second)
    loan_no = q(lambda: M.Loan.query.filter_by(customer_id=cid).first().loan_number)
    r = off.get(f"/customers/search?q={loan_no}", follow_redirects=False)
    check("Exact loan number opens the loan", r.status_code == 302 and "/loans/" in r.headers["Location"], r.headers.get("Location"))
    r = off.get("/customers/search?q=Walkin", follow_redirects=False)
    check("A single name match opens Customer 360", r.status_code == 302, r.status_code)
    post(admin, "/customers/new", {"name": "Walkin Second", "pan": "WALKS1234R", "mobile": "9811113333", "branch_id": b1, "gender": "Male"})
    r = off.get("/customers/search?q=Walkin", follow_redirects=False)
    check("Several matches still show the result list", r.status_code == 200 and "Walkin Second" in r.get_data(as_text=True))
    r = S["clerk"].get(f"/customers/search?q={loan_no}", follow_redirects=False)
    check("Direct hits respect permissions (clerk without loan access gets no loan redirect)",
          "/loans/" not in r.headers.get("Location", ""), r.status_code)

    # ---------------- plain-language limits ------------------------------------------------
    lid = q(lambda: M.Loan.query.filter_by(customer_id=cid).first().id)
    page = off.get(f"/loans/{lid}").get_data(as_text=True)
    check("Officer sees the amount due and who can collect it", "cannot post loan repayments" in page and "Due now" in page)
    page = sen.get(f"/loans/{lid}").get_data(as_text=True)
    check("Repayment dialog states amount, mode, customer and loan", "Collect {amount} by {mode} for Audit Customer, loan" in page)
    page = off.get(f"/transactions/new?account={acc_no}").get_data(as_text=True)
    check("Transaction dialog states type, amount, channel, account and customer",
          f"{{txn_type}} of {{amount}} by {{channel}} on account {acc_no} (Audit Customer)" in page)
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nux workflows: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
