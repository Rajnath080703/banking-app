"""Workflow coverage: tickets, alerts & notifications, scheduled scan, settings validation,
employee administration, and every rejection path of maker-checker."""
import os
import sys
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, app, approve_latest, balance, cleanup, client, db, deposit, invite_employee, post, q, setup_bank)

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin, clerk = S["officer"], S["senior"], S["manager"], S["admin"], S["clerk"]
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)

    # ---------------- tickets ----------------------------------------------------------
    post(clerk, "/tickets/new", {"customer": cid, "subject": "Debit card blocked", "category": "Cards", "priority": "High",
                                 "description": "Customer cannot use card"}, f"/tickets/new?customer={cid}")
    tid = q(lambda: M.Ticket.query.one().id)
    officer_id = q(lambda: M.Employee.query.filter_by(email="officer@bank.test").one().id)
    post(off, f"/tickets/{tid}", {"status": "In Progress", "assigned_to": officer_id, "note": "Called customer"})
    post(off, f"/tickets/{tid}", {"status": "Resolved", "note": "Card reissued"})
    t = q(lambda: db.session.get(M.Ticket, tid))
    notes = q(lambda: db.session.get(M.Ticket, tid).notes.count())
    check("Ticket raised, assigned, progressed and resolved with history", t.status == "Resolved" and t.resolved_at and notes == 3,
          f"{t.status}, {notes} notes")
    r = post(clerk, f"/tickets/{tid}", {"status": "Closed"})
    check("Clerk without tickets.edit cannot change a ticket", q(lambda: db.session.get(M.Ticket, tid).status) == "Resolved")
    check("Ticket short subject refused", "Please enter a subject" in post(clerk, "/tickets/new", {"customer": cid, "subject": "x"},
                                                                          f"/tickets/new?customer={cid}").get_data(as_text=True))

    # ---------------- alerts: raised, de-duplicated, routed, resolved -------------------
    deposit(off, acc_no, "600000")  # high-value rule (₹5 lakh) → fraud/risk alert, below approval limit
    n = q(lambda: M.Alert.query.filter_by(alert_type="fraud_risk").count())
    check("High-value transaction raises a fraud/risk alert", n == 1, n)
    with app.app_context():
        from bank.services import raise_alert
        a1 = raise_alert("system", "Test", "msg", reference="DUP-1")
        a2 = raise_alert("system", "Test", "msg", reference="DUP-1")
        db.session.commit()
        same = a1.id == a2.id
        dup = M.Alert.query.filter_by(reference="DUP-1").count()
    check("Same open alert is never stored twice", dup == 1 and same, dup)
    with app.app_context():
        raise_alert("security", "Staff security review needed", "msg", module="user_management", reference="SEC-1")
        db.session.commit()
    clerk_alerts = clerk.get("/alerts/").get_data(as_text=True)
    check("Alerts are routed by permission (clerk sees transaction alerts, not user-management ones)",
          "Staff security review needed" not in clerk_alerts and "High-value" in clerk_alerts
          and "Staff security review needed" in admin.get("/alerts/").get_data(as_text=True))
    aid_alert = q(lambda: M.Alert.query.filter_by(alert_type="fraud_risk").one().id)
    r = sen.get(f"/alerts/{aid_alert}/open", follow_redirects=False)
    check("Opening an alert marks it read and goes to the record", r.status_code == 302 and q(lambda: db.session.get(M.Alert, aid_alert).is_read))
    post(sen, f"/alerts/{aid_alert}/resolve", {}, "/alerts/")
    check("Alert resolved", q(lambda: db.session.get(M.Alert, aid_alert).is_resolved))
    post(sen, "/alerts/mark-read", {}, "/alerts/")
    rule_form = {f"r{i}_enabled": "on" for i in q(lambda: [r.id for r in M.AlertRule.query.all()])}  # the form is the full rule set
    rule_form.update({"r1_threshold": "750000", "r1_severity": "Critical", "r1_route": "transactions"})
    post(admin, "/alerts/rules", rule_form)
    rule = q(lambda: db.session.get(M.AlertRule, 1))
    check("Risk rule edited by an administrator", rule.threshold == Decimal("750000.00") and rule.severity == "Critical", rule.threshold)
    check("Officer cannot edit risk rules", post(off, "/alerts/rules", {"r1_threshold": "1"}, "/alerts/rules").status_code == 403)

    # ---------------- rejection paths ----------------------------------------------------
    before = balance(aid)
    post(off, "/deposits/new", {"customer": cid, "account_id": aid, "principal": "25000", "tenure_months": "6"},
         f"/deposits/new?customer={cid}")
    rid = q(lambda: M.ApprovalRequest.query.filter_by(request_type="fd_create", status="Pending Review").one().id)
    r = post(sen, f"/approvals/{rid}", {"action": "reject", "remarks": ""})
    check("Rejecting without remarks is refused", q(lambda: db.session.get(M.ApprovalRequest, rid).status) == "Pending Review")
    post(sen, f"/approvals/{rid}", {"action": "reject", "remarks": "Customer changed mind"})
    fd = q(lambda: M.FixedDeposit.query.filter_by(principal=Decimal("25000")).one())
    check("Rejected FD is refunded to the funding account", fd.status == "Rejected" and balance(aid) == before, f"{before} -> {balance(aid)}")
    post(off, "/loans/new", {"customer": cid, "loan_type": "vehicle", "account_id": aid, "amount": "300000",
                             "interest_rate": "9", "tenure_months": "36"}, f"/loans/new?customer={cid}")
    rid = q(lambda: M.ApprovalRequest.query.filter_by(request_type="loan_sanction", status="Pending Review").one().id)
    post(sen, f"/approvals/{rid}", {"action": "review", "remarks": "ok"})
    post(mgr, f"/approvals/{rid}", {"action": "reject", "remarks": "Income proof insufficient"})
    vl = q(lambda: M.Loan.query.filter_by(loan_type="vehicle").one())
    check("Manager rejection: loan Rejected, nothing disbursed", vl.status == "Rejected" and vl.outstanding == 0)
    with app.app_context():
        from bank.ledger import reconciliation
        check("Books reconcile after refunds and rejections", all(c["ok"] for c in reconciliation()))

    # ---------------- scheduled scan: overdue, EMI due, KYC expiry, dormancy -------------
    post(off, "/loans/new", {"customer": cid, "loan_type": "personal", "account_id": aid, "amount": "60000",
                             "interest_rate": "12", "tenure_months": "6"}, f"/loans/new?customer={cid}")
    approve_latest(S)
    lid = q(lambda: M.Loan.query.filter_by(loan_type="personal").one().id)
    with app.app_context():
        from bank.services import scan_scheduled_alerts
        from bank.utils import local_today
        loan = db.session.get(M.Loan, lid)
        loan.next_due_date = local_today() - timedelta(days=75)
        doc = M.Document.query.filter_by(doc_type="PAN Card").first()
        doc.expiry_date = local_today() - timedelta(days=1)
        acc = db.session.get(M.Account, aid)
        acc.last_txn_on = local_today() - timedelta(days=400)
        db.session.commit()
        scan_scheduled_alerts(force=True)
        loan = db.session.get(M.Loan, lid)
        check("Scan marks the loan overdue with day count", loan.status == "Overdue" and loan.overdue_days == 75, f"{loan.status} {loan.overdue_days}")
        check("Overdue alert High after 60 days", M.Alert.query.filter_by(alert_type="overdue_loan", severity="High").count() == 1)
        check("Expired KYC document flips customer KYC to Expired", db.session.get(M.Customer, cid).kyc_status == "Expired")
        check("Account idle 400 days becomes Dormant", db.session.get(M.Account, aid).status == "Dormant")
        again = scan_scheduled_alerts(force=True)
        check("Second scan raises no duplicate alerts", M.Alert.query.filter_by(alert_type="overdue_loan").count() == 1)
    deposit(off, acc_no, "100")
    check("Deposit reactivates a dormant account and raises a risk alert",
          q(lambda: db.session.get(M.Account, aid).status) == "Active" and
          q(lambda: M.Alert.query.filter(M.Alert.title == "Activity on dormant account").count()) == 1)

    # ---------------- settings validation -------------------------------------------------
    post(admin, "/settings/", {"session_timeout_minutes": "999", "max_failed_logins": "1", "bank_name": "Local Bank",
                               "large_txn_approval_limit": "500000", "penal_rate_pa": "24"})
    check("Out-of-range settings refused, valid ones saved",
          q(lambda: M.Setting.get("session_timeout_minutes")) == "15" and q(lambda: M.Setting.get("large_txn_approval_limit")) == "500000")
    check("Officer cannot change settings", post(off, "/settings/", {"bank_name": "X"}).status_code == 403)

    # ---------------- employee administration ---------------------------------------------
    temp = invite_employee(admin, "Temp Staff", "temp@bank.test", roles["Clerk"], b1)
    eid = q(lambda: M.Employee.query.filter_by(email="temp@bank.test").one().id)
    for action in ("revoke", "unlock", "reset_mfa"):
        post(admin, f"/users/{eid}/edit", {"action": action}, f"/users/{eid}/edit")
    check("Revoking sessions signs the employee out", temp.get("/", follow_redirects=False).status_code == 302)
    r = post(admin, f"/users/{eid}/edit", {"action": "reset"}, f"/users/{eid}/edit")
    check("Password reset issues a one-time link, old password stops working",
          "/invite/" in r.get_data(as_text=True) and client("temp@bank.test", "Strong#Pass2026").get("/", follow_redirects=False).status_code == 302)
    post(admin, "/users/roles/new", {"name": "Cash Officer", "description": "teller", "level": "35"}, "/users/roles")
    check("Custom role created", q(lambda: M.Role.query.filter_by(name="Cash Officer").count()) == 1)
    check("Employee cannot manage themselves or a senior role",
          post(off, f"/users/{q(lambda: M.Employee.query.filter_by(email='manager@bank.test').one().id)}/edit", {"action": "revoke"}).status_code == 403)
    sweep = {p: admin.get(p).status_code for p in ("/users/", "/users/roles", "/users/login-history?failed=1", f"/users/{eid}/edit",
                                                    "/settings/profile", "/settings/password", "/alerts/?show=all", "/alerts/?type=fraud_risk",
                                                    "/tickets/?status=Resolved", "/tickets/?mine=1", f"/tickets/{tid}", "/audit/?cat=auth",
                                                    "/accounts/?status=Dormant", "/kyc/?view=expired", "/approvals/?status=Rejected")}
    check("Administration and list pages load", all(v == 200 for v in sweep.values()), {k: v for k, v in sweep.items() if v != 200})
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nworkflows: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
