"""Batch 1 upgrades: printable transaction receipts and private search (no PAN / mobile / name in URLs).

Receipts: offered right after a deposit, withdrawal or transfer; PDF; permission and branch checks;
every print audited, reprints marked as copies. Search: boxes post the term and the app redirects to
an encrypted token that only the same employee can open; filters survive; old ?q= links still work."""
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (M, app, approve_latest, cleanup, db, deposit, form_token, invite_employee, post, q,  # noqa: E402
                     setup_bank)

from bank import private_search  # noqa: E402
from bank.money import amount_in_words  # noqa: E402

results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def prints(ref):
    return q(lambda: [a.details for a in M.AuditLog.query.filter_by(action="Receipt printed", reference=ref)
                      .order_by(M.AuditLog.id)])


def search(c, url, term, field="q", **extra):
    """Submit a search box the way the browser does: POST with CSRF and the _search marker."""
    t, _ = form_token(c, url)
    return c.post(url, data={"_csrf": t, "_search": field, field: term, **extra}, follow_redirects=False)


try:
    S, cid, aid, acc_no = setup_bank()
    off, sen, mgr, admin, clerk = S["officer"], S["senior"], S["manager"], S["admin"], S["clerk"]

    # ---------------- amount in words (Indian numbering) ----------------------------------
    words = {"1": "Rupees One Only", "0.50": "Rupees Zero and Fifty Paise Only",
             "1250.50": "Rupees One Thousand Two Hundred Fifty and Fifty Paise Only",
             "125000": "Rupees One Lakh Twenty Five Thousand Only", "10000000": "Rupees One Crore Only",
             "123456789.99": "Rupees Twelve Crore Thirty Four Lakh Fifty Six Thousand Seven Hundred Eighty Nine "
                             "and Ninety Nine Paise Only"}
    bad = {v: amount_in_words(v) for v, w in words.items() if amount_in_words(v) != w}
    check("Amount in words: rupees, paise, thousand, lakh, crore", not bad, bad)

    # ---------------- receipt after a deposit -------------------------------------------
    r = deposit(off, acc_no, "1250.50")
    body = r.get_data(as_text=True)
    txn = q(lambda: M.Transaction.query.filter_by(account_id=aid).order_by(M.Transaction.id.desc()).first())
    check("After a deposit the page offers the receipt for that transaction",
          "Print the receipt for the customer" in body and f"/transactions/{txn.id}/receipt" in body and txn.txn_ref in body)
    rr = off.get(f"/transactions/{txn.id}/receipt")
    check("Receipt is a PDF shown in the browser (to print)",
          rr.status_code == 200 and rr.mimetype == "application/pdf" and rr.data[:4] == b"%PDF"
          and "attachment" not in rr.headers.get("Content-Disposition", ""), rr.status_code)
    check("Printing is audited as copy 1", prints(txn.txn_ref) == ["copy 1"], prints(txn.txn_ref))
    off.get(f"/transactions/{txn.id}/receipt")
    check("A reprint is audited as copy 2 (the PDF says DUPLICATE)", prints(txn.txn_ref) == ["copy 1", "copy 2"])
    check("Recent transactions and Customer 360 rows have a receipt button",
          f"/transactions/{txn.id}/receipt" in off.get(f"/transactions/new?account={acc_no}").get_data(as_text=True)
          and f"/transactions/{txn.id}/receipt" in off.get(f"/customers/{cid}?tab=transactions").get_data(as_text=True))
    check("Unknown transaction: 404", off.get("/transactions/999999/receipt").status_code == 404)

    # ---------------- permissions and branches ------------------------------------------
    r = deposit(clerk, acc_no, "10")
    ctxn = q(lambda: M.Transaction.query.order_by(M.Transaction.id.desc()).first().id)
    check("Clerk (no 'print' permission): no receipt offered and direct link 403, nothing audited",
          "Print the receipt" not in r.get_data(as_text=True) and clerk.get(f"/transactions/{ctxn}/receipt").status_code == 403
          and len(prints(txn.txn_ref)) == 2)
    post(admin, "/settings/branches/new", {"code": "BR2", "name": "Branch Two"}, "/settings/")
    b2 = q(lambda: M.Branch.query.filter_by(code="BR2").one().id)
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    other = invite_employee(admin, "Officer Two", "officer2@bank.test", roles["Officer"], b2)
    check("Officer of another branch: 403 and no copy recorded",
          other.get(f"/transactions/{txn.id}/receipt").status_code == 403 and len(prints(txn.txn_ref)) == 2)
    forged = off.get(f"/transactions/new?account={acc_no}&receipt=999999&receipt=abc").get_data(as_text=True)
    check("A forged ?receipt= id shows no receipt button", "Print the receipt" not in forged)

    # ---------------- transfer: two receipts -------------------------------------------------
    post(off, "/accounts/new", {"customer": cid, "account_type": "Current"}, f"/accounts/new?customer={cid}")
    approve_latest(S)
    acc2 = q(lambda: M.Account.query.filter_by(customer_id=cid, account_type="Current").one().account_number)
    r = post(off, "/transactions/transfer", {"from": acc_no, "to": acc2, "amount": "100"},
             f"/transactions/transfer?from={acc_no}&to={acc2}")
    body = r.get_data(as_text=True)
    out, into = q(lambda: [(t.id, t.txn_type) for t in M.Transaction.query.order_by(M.Transaction.id.desc()).limit(2)])
    check("After a transfer both the debit and the credit receipt are offered",
          "Debit receipt" in body and "Credit receipt" in body
          and f"/transactions/{out[0]}/receipt" in body and f"/transactions/{into[0]}/receipt" in body)
    check("Transfer receipts print", all(off.get(f"/transactions/{t}/receipt").status_code == 200 for t in (out[0], into[0])))
    with app.app_context():
        from bank.ledger import reconciliation
        rec = reconciliation()
    check("Books still reconcile", all(c["ok"] for c in rec), [c["name"] for c in rec if not c["ok"]])

    # ---------------- private search -------------------------------------------------------
    r = search(off, "/customers/", "ABCDE1234F")
    loc = r.headers.get("Location", "")
    check("Customer search by PAN: redirect carries a token, not the PAN",
          r.status_code == 303 and "ABCDE1234F" not in loc and "s=" in loc, loc[:80])
    r2 = off.get(loc)
    check("Opening the token finds the customer (single match goes to the profile)",
          r2.status_code == 302 and f"/customers/{cid}" in r2.headers.get("Location", ""))
    # Without a usable term the page lists recent customers (200) instead of jumping to the match (302).
    check("The same token opened by another employee does not search", sen.get(loc).status_code == 200)
    tampered = loc[:-3] + ("AAA" if not loc.endswith("AAA") else "BBB")
    check("A tampered token is ignored (no error, no search)", off.get(tampered).status_code == 200)
    r = search(off, "/customers/search", "9876543210")
    loc = r.headers.get("Location", "")
    check("Global search by mobile: no number in the URL", r.status_code == 303 and "9876543210" not in loc and "s=" in loc, loc[:80])
    check("Global search result opens the customer", f"/customers/{cid}" in off.get(loc).headers.get("Location", ""))
    r = search(off, "/transactions/", "Audit Customer", type="Credit", range="month")
    loc = r.headers.get("Location", "")
    args = parse_qs(urlparse(loc).query)
    check("Filters on the same form are kept beside the token",
          args.get("type") == ["Credit"] and args.get("range") == ["month"] and "q" not in args and "s" in args, args.keys())
    check("Filtered list shows the customer's transactions", "Audit Customer" in off.get(loc).get_data(as_text=True))
    r = search(off, "/accounts/", "ABCDE1234F")
    page = off.get(r.headers["Location"]).get_data(as_text=True)
    check("Status tab links on the result page carry a token, never the PAN", "ABCDE1234F" not in page.split("<main", 1)[-1]
          .replace('value="ABCDE1234F"', "") and "s=" in page)
    r = search(off, "/accounts/new", "ABCDE1234F", field="find")
    loc = r.headers.get("Location", "")
    check("Customer picker: term travels as a token (fs=)", "ABCDE1234F" not in loc and "fs=" in loc, loc[:80])
    check("Picker lists the customer to select", f"customer={cid}" in off.get(loc).get_data(as_text=True))
    check("Search POST without the CSRF token is refused",
          off.post("/customers/", data={"_search": "q", "q": "ABCDE1234F"}).status_code == 400)
    check("Old-style ?q= links keep working", off.get("/customers/?q=ABCDE1234F").status_code == 302)
    old_ttl = private_search.TOKEN_TTL
    private_search.TOKEN_TTL = -1
    try:
        expired = search(off, "/customers/", "ABCDE1234F").headers["Location"]
        check("An expired token finds nothing", off.get(expired).status_code == 200)
    finally:
        private_search.TOKEN_TTL = old_ttl
    html = off.get("/customers/").get_data(as_text=True)
    check("Search boxes post the term (method=post with the _search marker)",
          'name="_search" value="q"' in html and 'method="post" role="search"' in html)
    logged = q(lambda: [a.details for a in M.AuditLog.query.filter(M.AuditLog.action.in_(("Customer search", "Global search")))])
    check("Audit log records searches without the term", logged and not any("ABCDE1234F" in (d or "") or "9876543210" in (d or "")
                                                                              for d in logged))
finally:
    cleanup()

failed = [t for t, ok in results if not ok]
print(f"\nreceipts & private search: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
