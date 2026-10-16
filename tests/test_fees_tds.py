"""Batch 2 upgrades: fees & charges (service fees, GST, due fees, monthly minimum-balance charge,
waivers through maker-checker, the fee schedule) and TDS on fixed-deposit interest (threshold,
PAN / no-PAN rates, senior citizens, Form 15G/15H, yearly aggregation), with the books reconciling."""
import os
import sys
from datetime import date, timedelta
from decimal import Decimal as Dec

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, app, approve_latest, balance, cleanup, db, deposit, invite_employee, post, q,  # noqa: E402
                     setup_bank)

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def fee_id(code):
    return q(lambda: M.FeeType.query.filter_by(code=code).one().id)


def charges(**kw):
    return q(lambda: [(c.status, c.total) for c in M.FeeCharge.query.filter_by(**kw).order_by(M.FeeCharge.id)])


def gl(code):
    """Credit-minus-debit movement on a GL account."""
    with app.app_context():
        from bank.ledger import gl_balance
        dr, cr = gl_balance(code)
        return cr - dr


def books_ok():
    with app.app_context():
        from bank.ledger import reconciliation
        bad = [c["name"] for c in reconciliation() if not c["ok"]]
    return not bad, bad


def open_account(S, cid, kind):
    post(S["officer"], "/accounts/new", {"customer": cid, "account_type": kind}, f"/accounts/new?customer={cid}")
    approve_latest(S)
    return q(lambda: (lambda a: (a.id, a.account_number))(
        M.Account.query.filter_by(customer_id=cid, account_type=kind).order_by(M.Account.id.desc()).first()))


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin, clerk = S["officer"], S["senior"], S["manager"], S["admin"], S["clerk"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)
    deposit(off, acc_no, "5000")

    # ---------------- fee schedule ----------------------------------------------------------
    codes = q(lambda: {t.code: (t.kind, t.calc) for t in M.FeeType.query.all()})
    check("Starter fee schedule is seeded (service fees + monthly minimum balance)",
          {"CHQBOOK", "DUPSTMT", "DDISSUE", "MINBAL"} <= set(codes) and codes["MINBAL"] == ("min_balance", "flat"), codes)
    check("New GL accounts exist: 4200 fee income, 2300 GST payable, 2400 TDS payable",
          q(lambda: {g.code for g in M.GLAccount.query.filter(M.GLAccount.code.in_(["4200", "2300", "2400"]))}) == {"4200", "2300", "2400"})

    # ---------------- charging a service fee ----------------------------------------------
    r = post(off, "/fees/charge", {"account": acc_no, "fee_type": fee_id("CHQBOOK")}, f"/fees/charge?account={acc_no}")
    body = r.get_data(as_text=True)
    check("Cheque book fee: ₹100 + 18% GST = ₹118 debited, receipt offered",
          balance(aid) == Dec("4882.00") and charges(account_id=aid) == [("Charged", Dec("118.00"))]
          and "Print the receipt" in body, balance(aid))
    check("Ledger: fee income +₹100, GST payable +₹18", gl("4200") == Dec("100.00") and gl("2300") == Dec("18.00"),
          (gl("4200"), gl("2300")))
    post(off, "/fees/charge", {"account": acc_no, "fee_type": fee_id("DDISSUE"), "base": "100000"}, f"/fees/charge?account={acc_no}")
    post(off, "/fees/charge", {"account": acc_no, "fee_type": fee_id("DDISSUE"), "base": "1000"}, f"/fees/charge?account={acc_no}")
    totals = [t for s, t in charges(account_id=aid)]
    check("Percentage fee: 0.25% of ₹1,00,000 = ₹250 + GST; small amount uses the ₹25 minimum + GST",
          totals[1:] == [Dec("295.00"), Dec("29.50")], totals)
    r = post(off, "/fees/charge", {"account": acc_no, "fee_type": fee_id("DDISSUE")}, f"/fees/charge?account={acc_no}")
    check("Percentage fee without the amount: clear message, nothing charged",
          "Enter the amount the" in r.get_data(as_text=True) and len(charges(account_id=aid)) == 3)
    r = post(off, "/fees/charge", {"account": acc_no, "fee_type": fee_id("MINBAL")}, f"/fees/charge?account={acc_no}")
    check("The monthly minimum-balance charge cannot be charged by hand", "Choose a fee from the list" in r.get_data(as_text=True))

    # ---------------- due fee collected from the next credit ------------------------------
    aid2, acc2 = open_account(S, cid, "Current")
    r = post(off, "/fees/charge", {"account": acc2, "fee_type": fee_id("CHQBOOK")}, f"/fees/charge?account={acc2}")
    check("Balance too low: fee recorded as Due, balance stays ₹0, no ledger entry",
          "recorded as due" in r.get_data(as_text=True) and balance(aid2) == 0
          and charges(account_id=aid2) == [("Due", Dec("118.00"))] and gl("4200") == Dec("375.00"))
    deposit(off, acc2, "50")
    check("A credit too small to cover it leaves the fee due", balance(aid2) == Dec("50.00") and charges(account_id=aid2)[0][0] == "Due")
    deposit(off, acc2, "100")
    check("The next credit that covers it collects it automatically",
          balance(aid2) == Dec("32.00") and charges(account_id=aid2) == [("Charged", Dec("118.00"))], balance(aid2))
    ok, bad = books_ok()
    check("Books reconcile after fees", ok, bad)

    # ---------------- permissions and branches -----------------------------------------------
    accountant = invite_employee(admin, "Accountant", "acct@bank.test", roles["Accountant"], b1)
    check("Accountant (no transactions create): charge page 403", accountant.get("/fees/charge").status_code == 403)
    check("Clerk (front desk) can charge service fees", clerk.get(f"/fees/charge?account={acc_no}").status_code == 200)
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    other = invite_employee(admin, "Officer Two", "officer2@bank.test", roles["Officer"], b2)
    r = post(other, "/fees/charge", {"account": acc_no, "fee_type": fee_id("CHQBOOK")}, f"/fees/charge?account={acc_no}")
    check("Officer of another branch: account not found, nothing charged",
          "not found in your branch" in r.get_data(as_text=True) and len(charges(account_id=aid)) == 3)
    check("Other branch sees none of these charges", acc_no not in other.get("/fees/").get_data(as_text=True))

    # ---------------- waivers (maker-checker) -------------------------------------------------
    first = q(lambda: M.FeeCharge.query.filter_by(account_id=aid).order_by(M.FeeCharge.id).first().id)
    r = post(off, f"/fees/{first}/waive", {"reason": "no"}, "/fees/")
    check("Waiver needs a reason", "reason for the waiver" in r.get_data(as_text=True))
    before = balance(aid)
    post(off, f"/fees/{first}/waive", {"reason": "Bank error, cheque book reissued"}, "/fees/")
    check("Waiver request changes nothing until approved", balance(aid) == before
          and q(lambda: db.session.get(M.FeeCharge, first).status) == "Charged")
    r = post(off, f"/fees/{first}/waive", {"reason": "Second try at the same thing"}, "/fees/")
    check("A second waiver request for the same fee is refused", "already waiting for approval" in r.get_data(as_text=True))
    rid = q(lambda: M.ApprovalRequest.query.filter_by(request_type="fee_waiver").one().id)
    check("The maker cannot review their own waiver", post(off, f"/approvals/{rid}", {"action": "review", "remarks": "ok"}).status_code in (403, 200)
          and q(lambda: db.session.get(M.ApprovalRequest, rid).status) == "Pending Review")
    approve_latest(S, rid)
    check("Approved waiver refunds ₹118 and reverses fee income and GST",
          balance(aid) == before + Dec("118.00") and q(lambda: db.session.get(M.FeeCharge, first).status) == "Waived"
          and gl("4200") == Dec("475.00") - Dec("100.00") and gl("2300") == Dec("85.50") - Dec("18.00"),
          (balance(aid), gl("4200"), gl("2300")))
    post(off, "/fees/charge", {"account": acc2, "fee_type": fee_id("DUPSTMT")}, f"/fees/charge?account={acc2}")
    due = q(lambda: M.FeeCharge.query.filter_by(account_id=aid2, status="Due").one().id)
    post(off, f"/fees/{due}/waive", {"reason": "Customer goodwill gesture"}, "/fees/")
    approve_latest(S)
    check("Waiving a due fee cancels it without moving money",
          q(lambda: db.session.get(M.FeeCharge, due).status) == "Waived" and balance(aid2) == Dec("32.00"))
    deposit(off, acc2, "500")
    check("A waived fee is never collected later", balance(aid2) == Dec("532.00"))

    # ---------------- fee schedule administration -------------------------------------------
    page = off.get("/fees/types")
    sc = post(off, "/fees/types", {"code": "X1", "name": "Sneaky fee", "calc": "flat", "amount": "1", "kind": "manual"},
              "/fees/" if page.status_code == 403 else "/fees/types", follow=False).status_code
    check("Officer sees no edit form and cannot change fees",
          (page.status_code == 403 or "Save fee" not in page.get_data(as_text=True)) and sc == 403
          and not q(lambda: M.FeeType.query.filter_by(code="X1").first()), (page.status_code, sc))
    r = post(admin, "/fees/types", {"code": "SMS", "name": "SMS alerts (quarterly)", "calc": "percent", "kind": "manual", "gst_pct": "18"},
             "/fees/types")
    check("Invalid fee (percentage without a value) is refused", "Enter the percentage" in r.get_data(as_text=True)
          and not q(lambda: M.FeeType.query.filter_by(code="SMS").first()))
    post(admin, "/fees/types", {"code": "SMS", "name": "SMS alerts (quarterly)", "calc": "flat", "amount": "15",
                                "kind": "manual", "gst_pct": "18", "active": "1"}, "/fees/types")
    sms = q(lambda: M.FeeType.query.filter_by(code="SMS").first())
    check("Admin adds a fee; it is audited", sms and sms.amount == Dec("15.00")
          and q(lambda: M.AuditLog.query.filter_by(action="Fee schedule changed", reference="SMS").count()) == 1)
    post(admin, "/fees/types", {"id": sms.id, "name": "SMS alerts (quarterly)", "calc": "flat", "amount": "20",
                                "kind": "manual", "gst_pct": "18"}, "/fees/types")
    check("Editing a fee and switching it off", q(lambda: (lambda t: (t.amount, t.active))(db.session.get(M.FeeType, sms.id)))
          == (Dec("20.00"), False))
    check("A switched-off fee is not offered to tellers", "SMS alerts" not in off.get(f"/fees/charge?account={acc_no}").get_data(as_text=True))

    # ---------------- monthly minimum-balance charge ----------------------------------------
    post(off, "/customers/new", {"name": "Low Balance", "pan": "LOWBA1234L", "mobile": "9400000011", "branch_id": b1, "gender": "Male"})
    cid2 = q(lambda: M.Customer.query.filter_by(name="Low Balance").one().id)
    with app.app_context():
        db.session.get(M.Customer, cid2).kyc_status = "Verified"
        db.session.commit()
    low_id, low_no = open_account(S, cid2, "Savings")
    deposit(off, low_no, "500")
    post(off, "/customers/new", {"name": "Tiny Balance", "pan": "TINYB1234T", "mobile": "9400000012", "branch_id": b1, "gender": "Male"})
    cid3 = q(lambda: M.Customer.query.filter_by(name="Tiny Balance").one().id)
    with app.app_context():
        db.session.get(M.Customer, cid3).kyc_status = "Verified"
        db.session.commit()
    tiny_id, tiny_no = open_account(S, cid3, "Savings")
    deposit(off, tiny_no, "50")
    later = date.today().replace(day=1) + timedelta(days=62)  # a month after this one is over
    with app.app_context():
        from bank.charges import charge_monthly_fees
        made = charge_monthly_fees(later)
        db.session.commit()
        made_again = charge_monthly_fees(later)
        db.session.commit()
    period = f"{later.replace(day=1) - timedelta(days=1):%Y-%m}"
    check("Average balance ₹500 < ₹1,000: ₹118 minimum-balance charge",
          charges(account_id=low_id) == [("Charged", Dec("118.00"))] and balance(low_id) == Dec("382.00"), charges(account_id=low_id))
    check("Balance ₹50 cannot cover it: Skipped, balance untouched (never negative)",
          charges(account_id=tiny_id) == [("Skipped", Dec("118.00"))] and balance(tiny_id) == Dec("50.00"))
    check("Accounts above the minimum are not charged", not q(lambda: M.FeeCharge.query.filter_by(account_id=aid, period=period).count()))
    check("Current accounts are not charged", not q(lambda: M.FeeCharge.query.filter_by(account_id=aid2, period=period).count()))
    check("Running it again for the same month charges nothing more", made == 2 and made_again == 0, (made, made_again))
    with app.app_context():
        this_month = charge_monthly_fees(date.today())  # previous month: these accounts did not exist yet
        db.session.commit()
    check("Accounts opened after the month began are not charged for it", this_month == 0, this_month)
    ok, bad = books_ok()
    check("Books reconcile after monthly charges and waivers", ok, bad)

    # ---------------- TDS rules -------------------------------------------------------------
    with app.app_context():
        from bank.charges import financial_year, is_senior, tds_quote
        c = db.session.get(M.Customer, cid)
        fy_checks = financial_year(date(2026, 3, 31)) == "2025-26" and financial_year(date(2026, 4, 1)) == "2026-27"
        c.dob = date(1967, 3, 31)
        senior_edge = is_senior(c, "2026-27") and not is_senior(c, "2025-26")
        c.dob = date(1990, 1, 1)
        below = tds_quote(c, Dec("40000"))
        above = tds_quote(c, Dec("60000"))
        c.dob = date(1950, 1, 1)
        senior_q = tds_quote(c, Dec("60000"))
        c.dob = date(1990, 1, 1)
        c.pan = ""
        no_pan = tds_quote(c, Dec("60000"))
        db.session.rollback()
    check("Financial year runs April to March", fy_checks)
    check("Senior citizen = 60 by 31 March at the end of the year", senior_edge)
    check("Interest within ₹50,000 a year: no TDS", below["tds"] == 0 and "threshold" in below["reason"], below)
    check("Above the threshold: 10% on the whole year's interest", above["tds"] == Dec("6000.00"), above["tds"])
    check("Senior citizen: ₹1,00,000 threshold, so ₹60,000 is not taxed", senior_q["tds"] == 0, senior_q)
    check("No valid PAN: 20%", no_pan["tds"] == Dec("12000.00") and not no_pan["pan_ok"], no_pan["tds"])

    # ---------------- TDS on a real FD payout ------------------------------------------------
    deposit(off, acc_no, "900000")
    post(off, "/deposits/new", {"customer": cid, "account_id": aid, "principal": "800000", "tenure_months": "12"},
         f"/deposits/new?customer={cid}")
    approve_latest(S)
    with app.app_context():
        fd = M.FixedDeposit.query.filter_by(principal=Dec("800000")).one()
        fd.maturity_date = date.today() - timedelta(days=1)
        db.session.commit()
        fd_id, interest = fd.id, fd.maturity_amount - fd.principal
        before = db.session.get(M.Account, aid).balance
        from bank.services import scan_scheduled_alerts
        scan_scheduled_alerts(force=True)
        after = db.session.get(M.Account, aid).balance
        row = M.InterestTds.query.filter_by(fd_id=fd_id).one()
        tds_txn = db.session.get(M.Transaction, row.txn_id) if row.txn_id else None
    expected = (interest * Dec("0.10")).quantize(Dec("0.01"))
    check("FD maturity: interest above ₹50,000 has 10% TDS deducted", row.tds == expected and row.interest == interest,
          f"interest {interest}, tds {row.tds}")
    check("Statement shows gross proceeds and a separate TDS debit",
          tds_txn is not None and tds_txn.category == "TDS" and tds_txn.amount == expected
          and after == before + Dec("800000") + interest - expected, (before, after))
    check("TDS payable GL holds the tax", gl("2400") == expected, gl("2400"))
    ok, bad = books_ok()
    check("Books reconcile after the payout", ok, bad)

    # ---------------- yearly aggregation and Form 15G/15H ---------------------------------------
    with app.app_context():
        c = db.session.get(M.Customer, cid)
        nxt = tds_quote(c, Dec("1000"))
    check("Later interest the same year: taxed at once (already above the threshold)", nxt["tds"] == Dec("100.00"), nxt["tds"])
    fy = q(lambda: financial_year(date.today()))
    post(sen, f"/customers/{cid}/edit", {"mobile": "9876543210", "email": "", "address": "", "city": "", "occupation": "",
                                        "risk_category": "Low", "tds_exempt_fy": fy})
    rid = q(lambda: M.ApprovalRequest.query.filter_by(request_type="customer_update", status="Pending Review").one().id)
    check("Form 15G/15H is a maker-checker change", q(lambda: db.session.get(M.Customer, cid).tds_exempt_fy) is None)
    post(S["manager2"], f"/approvals/{rid}", {"action": "review", "remarks": "form seen"})
    post(mgr, f"/approvals/{rid}", {"action": "approve", "remarks": "ok"})
    with app.app_context():
        c = db.session.get(M.Customer, cid)
        exempt = tds_quote(c, Dec("1000"))
    check("After approval: no TDS for this financial year", q(lambda: db.session.get(M.Customer, cid).tds_exempt_fy) == fy
          and exempt["tds"] == 0 and "15" in exempt["reason"], exempt["reason"])
    r = post(sen, f"/customers/{cid}/edit", {"mobile": "9876543210", "risk_category": "Low", "tds_exempt_fy": "2019-20"})
    check("A declaration for another year is refused", "current financial year" in r.get_data(as_text=True))

    # ---------------- database protections, settings and reports ---------------------------
    with app.app_context():
        try:
            db.session.execute(db.text("UPDATE interest_tds SET tds = 0"))
            db.session.commit()
            blocked = False
        except Exception:  # noqa: BLE001 - permission denied is the expected outcome
            db.session.rollback()
            blocked = True
    check("Runtime role cannot rewrite TDS records", blocked)
    page = admin.get("/settings/").get_data(as_text=True)
    check("TDS rates and thresholds are editable in Settings", "tds_threshold_senior" in page and "tds_rate_no_pan_pct" in page)
    r_f = off.get("/reports/fees?range=year").get_data(as_text=True)
    r_t = admin.get("/reports/tds?range=year").get_data(as_text=True)
    check("Fees and TDS reports list the entries", "Cheque book" in r_f and "TDS on FD Interest" in r_t and fy in r_t)
    check("TDS report masks the PAN", "ABCDE1234F" not in r_t)
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nfees & tds: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
