"""Shared setup for the test suites.

Every suite gets a throwaway PostgreSQL database built by the real Alembic migrations, and the
app under test connects as the restricted runtime role `bank_app` — exactly like production.
Staff are created through the UI and set their passwords through one-time invite links.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import dotenv_values  # noqa: E402

env = {**dotenv_values(os.path.join(ROOT, ".env")), **{k: v for k, v in os.environ.items() if k.startswith(("BANK_", "DATABASE_URL", "POSTGRES_"))}}
DBNAME = os.environ.get("BANK_TEST_DB", "bankdb_audit")
OWNER_BASE = env["BANK_DB_OWNER_URL"].rsplit("/", 1)[0]
APP_BASE = env["DATABASE_URL"].rsplit("/", 1)[0]


def psql(sql):
    """Admin statements (CREATE/DROP DATABASE) over a normal owner connection — works locally and in CI."""
    from sqlalchemy import create_engine, text as _text
    eng = create_engine(env["BANK_DB_OWNER_URL"].rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(_text(sql))
    eng.dispose()


psql(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
psql(f"CREATE DATABASE {DBNAME}")
os.environ["DATABASE_URL"] = f"{APP_BASE}/{DBNAME}"
if not (os.environ.get("BANK_ADMIN_PASSWORD") or env.get("BANK_ADMIN_PASSWORD")):
    sys.exit("Set BANK_ADMIN_PASSWORD (in .env or the environment) before running the tests.")
os.environ.setdefault("BANK_ADMIN_PASSWORD", env.get("BANK_ADMIN_PASSWORD") or "")
os.environ["BANK_INPROCESS_SCAN"] = "0"  # tests run the scan explicitly

from bank.bootstrap import prepare_database  # noqa: E402

prepare_database(owner_url=f"{OWNER_BASE}/{DBNAME}", app_password=env["BANK_APP_DB_PASSWORD"])

from bank import create_app  # noqa: E402
from bank import models as M  # noqa: E402
from bank.extensions import db, limiter  # noqa: E402

app = create_app({"RATELIMIT_ENABLED": os.environ.get("BANK_TEST_RATELIMIT", "0") == "1"})
limiter.enabled = app.config["RATELIMIT_ENABLED"]
app.config["PROPAGATE_EXCEPTIONS"] = False

ADMIN = ("rajnath080703@gmail.com", os.environ["BANK_ADMIN_PASSWORD"])
PW = "Strong#Pass2026"


def tok(html):
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else None


def idem(html):
    m = re.search(r'name="_idem" value="([^"]+)"', html)
    return m.group(1) if m else ""


def client(email, pw):
    c = app.test_client()
    t = tok(c.get("/login").get_data(as_text=True))
    r = c.post("/login", data={"email": email, "password": pw, "_csrf": t}, follow_redirects=True)
    if "/settings/password" in r.request.path:
        t = tok(r.get_data(as_text=True))
        c.post("/settings/password", data={"_csrf": t, "current": pw, "new": pw + "x", "confirm": pw + "x"})
    return c


def form_token(c, url):
    html = c.get(url).get_data(as_text=True)
    return tok(html), html


def post(c, url, data, get_url=None, follow=True, files=None):
    t, html = form_token(c, get_url or url)
    payload = dict(data, _csrf=t)
    if "_idem" not in payload:
        payload["_idem"] = idem(html)
    if files:
        payload.update(files)
        return c.post(url, data=payload, follow_redirects=follow, content_type="multipart/form-data")
    return c.post(url, data=payload, follow_redirects=follow)


def q(fn):
    with app.app_context():
        return fn()


def invite_employee(admin, name, email, role_id, branch_id):
    """Create an employee through the UI and set their password with the invite link."""
    r = post(admin, "/users/new", {"name": name, "email": email, "role_id": role_id, "branch_id": branch_id})
    html = r.get_data(as_text=True)
    m = re.search(r'value="(http[^"]*/invite/[^"]+)"', html)
    assert m, "invite link not shown: " + html[:300]
    path = "/invite/" + m.group(1).rsplit("/invite/", 1)[1]
    anon = app.test_client()
    r = post(anon, path, {"password": PW, "confirm": PW})
    assert "Your password is set" in r.get_data(as_text=True), r.get_data(as_text=True)[:400]
    return client(email, PW)


def upload(c, cid, doc_type, number, content=b"%PDF-1.4 scanned document", filename="scan.pdf"):
    return post(c, "/kyc/upload", {"customer": cid, "doc_type": doc_type, "doc_number": number},
                f"/kyc/upload?customer={cid}", files={"file": (io.BytesIO(content), filename)})


def approve_latest(staff, rid=None):
    rid = rid or q(lambda: M.ApprovalRequest.query.filter_by(status="Pending Review")
                   .order_by(M.ApprovalRequest.id.desc()).first().id)
    post(staff["senior"], f"/approvals/{rid}", {"action": "review", "remarks": "ok"})
    post(staff["manager"], f"/approvals/{rid}", {"action": "approve", "remarks": "ok"})
    return rid


def setup_bank():
    """Staff for every key role, one KYC-verified customer with an active savings account."""
    admin = client(*ADMIN)
    roles = q(lambda: {r.name: r.id for r in M.Role.query.all()})
    b1 = q(lambda: M.Branch.query.first().id)
    staff = {"admin": admin}
    for key, role in [("officer", "Officer"), ("senior", "Senior Officer"), ("manager", "Branch Manager"),
                      ("manager2", "Branch Manager"), ("clerk", "Clerk")]:
        staff[key] = invite_employee(admin, f"{role} {key}", f"{key}@bank.test", roles[role], b1)
    post(admin, "/customers/new", {"name": "Audit Customer", "pan": "ABCDE1234F", "mobile": "9876543210",
                                   "branch_id": b1, "gender": "Female"})
    cid = q(lambda: M.Customer.query.first().id)
    for dt, no in [("PAN Card", "ABCDE1234F"), ("Aadhaar", "123412341234")]:
        upload(admin, cid, dt, no)
    for did in q(lambda: [d.id for d in M.Document.query.all()]):
        post(staff["senior"], f"/kyc/doc/{did}/verify", {"decision": "approve"}, f"/kyc/doc/{did}")
    post(admin, "/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
    approve_latest(staff)
    acc = q(lambda: M.Account.query.first())
    return staff, cid, acc.id, acc.account_number


def deposit(c, acc_no, amount, ttype="Credit"):
    return post(c, f"/transactions/new?account={acc_no}", {"txn_type": ttype, "amount": str(amount), "channel": "Cash"})


def balance(aid):
    return q(lambda: db.session.get(M.Account, aid).balance)


def cleanup():
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    psql(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
