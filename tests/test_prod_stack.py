"""Production-like stack, tested only through HTTPS (Caddy → 2 gunicorn app servers → PostgreSQL,
Redis). Start it first:
    docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d --build
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
from decimal import Decimal

import requests
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dotenv_values(os.path.join(ROOT, "deploy", ".env.prod"))
BASE = "https://localhost:8443"
COMPOSE = ["docker", "compose", "-f", os.path.join(ROOT, "deploy", "docker-compose.prod.yml"),
           "--env-file", os.path.join(ROOT, "deploy", ".env.prod")]
results = []


def check(title, ok, detail=""):
    results.append((title, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail}")


def sh(*args):
    return subprocess.run(COMPOSE + list(args), capture_output=True, text=True)


def psql(sql, user="bankowner", password=None):
    env = ["-e", f"PGPASSWORD={password or ENV['DB_OWNER_PASSWORD']}"]
    r = subprocess.run(COMPOSE + ["exec", "-T"] + env + ["db", "psql", "-h", "127.0.0.1", "-U", user, "-d", "bankdb",
                                                          "-At", "-c", sql], capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip()


# Trust Caddy's local root certificate properly (no verify=False).
ca = os.path.join(tempfile.gettempdir(), "localbank-caddy-root.crt")
subprocess.run(COMPOSE + ["cp", "proxy:/data/caddy/pki/authorities/local/root.crt", ca], check=True, capture_output=True)


def session():
    s = requests.Session()
    s.verify = ca
    return s


def csrf(html):
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def idem(html):
    m = re.search(r'name="_idem" value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(email, pw):
    s = session()
    r = s.get(BASE + "/login")
    s.post(BASE + "/login", data={"_csrf": csrf(r.text), "email": email, "password": pw})
    return s


def post(s, url, data, get_url=None, files=None):
    html = s.get(BASE + (get_url or url)).text
    return s.post(BASE + url, data=dict(data, _csrf=csrf(html), _idem=idem(html)), files=files)


# ---- transport security --------------------------------------------------------------
r = session().get(BASE + "/readyz")
check("HTTPS with a verified certificate chain; readiness reports every dependency", r.ok and r.json()["status"] == "ready",
      r.json())
try:
    requests.get("http://localhost:8000/healthz", timeout=2)
    check("App servers are not reachable directly from the host", False)
except requests.exceptions.ConnectionError:
    check("App servers are not reachable directly from the host", True)
out, _ = psql("select 1")
port_open = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Test-NetConnection 127.0.0.1 -Port 5432 -WarningAction SilentlyContinue).TcpTestSucceeded"],
                           capture_output=True, text=True).stdout.strip()
published = sh("port", "db", "5432").stdout.strip()
check("Production database port is not published", published in ("", ":0"), published or "not published")

s = session()
r = s.get(BASE + "/login")
h = r.headers
check("HSTS, CSP and frame protection on every response",
      "max-age=31536000" in h.get("Strict-Transport-Security", "") and "frame-ancestors 'none'" in h.get("Content-Security-Policy", "")
      and h.get("X-Frame-Options") == "DENY", {k: h.get(k) for k in ("Strict-Transport-Security", "X-Frame-Options")})
cookie = r.headers.get("Set-Cookie", "")
check("Session cookie is __Host-, Secure, HttpOnly, SameSite", cookie.startswith("__Host-bank_session") and "Secure" in cookie
      and "HttpOnly" in cookie and "SameSite=Lax" in cookie, cookie.split(";")[0][:30] + "…")
check("Server software banner removed", "Server" not in h or "Caddy" not in h.get("Server", ""), h.get("Server"))

# ---- load balancing & stateless sessions ----------------------------------------------
instances = {session().get(BASE + "/healthz").headers.get("X-Instance") for _ in range(20)}
check("Requests are spread across both app servers", len(instances) == 2, instances)
admin = login(ENV["BANK_ADMIN_EMAIL"], ENV["BANK_ADMIN_PASSWORD"])
seen, ok = set(), True
for _ in range(10):
    r = admin.get(BASE + "/", allow_redirects=False)
    seen.add(r.headers.get("X-Instance"))
    ok = ok and r.status_code == 200
check("A signed-in session works on whichever server answers", ok and len(seen) == 2, seen)

# ---- client IP cannot be spoofed through the proxy ------------------------------------
spoof = session()
r = spoof.get(BASE + "/login")
spoof.post(BASE + "/login", data={"_csrf": csrf(r.text), "email": "spoof@y.z", "password": "bad"},
           headers={"X-Forwarded-For": "6.6.6.6"})
ip, _ = psql("select ip_address from login_history where email='spoof@y.z' order by id desc limit 1")
check("Forged X-Forwarded-For is not recorded as the client IP", ip and ip != "6.6.6.6", ip)

# ---- shared rate limit (Redis) across servers -----------------------------------------
attacker = session()
codes = []
for _ in range(12):
    r = attacker.get(BASE + "/login")
    codes.append(attacker.post(BASE + "/login", data={"_csrf": csrf(r.text), "email": "x@y.z", "password": "bad"}).status_code)
first = codes.index(429) if 429 in codes else None
check("Sign-in throttling (10/min per IP) is shared across both servers", first is not None and first <= 10, codes)
sh("exec", "-T", "redis", "sh", "-c", 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHDB')  # let the workflow sign in

# ---- a real workflow over HTTPS, with concurrent postings split across servers --------
roles = dict(line.split("|")[::-1] for line in psql("select id, name from role")[0].splitlines())
branch = psql("select id from branch order by id limit 1")[0]


def invite(name, email, role):
    r = post(admin, "/users/new", {"name": name, "email": email, "role_id": roles[role], "branch_id": branch})
    link = re.search(r'value="(https?://[^"]*/invite/[^"]+)"', r.text).group(1)
    path = link.split("localhost", 1)[1].split("/", 1)[1]
    anon = session()
    pw = "Strong#Pass2026"
    post(anon, "/" + path, {"password": pw, "confirm": pw})
    return login(email, pw)


suffix = os.urandom(3).hex()
senior = invite("Senior Prod", f"senior.{suffix}@bank.test", "Senior Officer")
manager = invite("Manager Prod", f"manager.{suffix}@bank.test", "Branch Manager")
pan = "PRODC" + str(int(suffix, 16) % 9000 + 1000) + "Z"
post(admin, "/customers/new", {"name": "Prod Stack Customer", "pan": pan, "mobile": "9" + str(int(suffix, 16) % 10**9).zfill(9),
                               "branch_id": branch, "gender": "Male"})
cid = psql("select id from customer where name='Prod Stack Customer' order by id desc limit 1")[0]
for dt, no in (("PAN Card", pan), ("Aadhaar", "111122223333")):
    post(admin, "/kyc/upload", {"customer": cid, "doc_type": dt, "doc_number": no}, f"/kyc/upload?customer={cid}",
         files={"file": ("scan.pdf", b"%PDF-1.4 prod stack", "application/pdf")})
for did in psql(f"select id from document where customer_id={cid}")[0].splitlines():
    post(senior, f"/kyc/doc/{did}/verify", {"decision": "approve"}, f"/kyc/doc/{did}")
post(admin, "/accounts/new", {"customer": cid, "account_type": "Savings"}, f"/accounts/new?customer={cid}")
rid = psql(f"select id from approval_request where customer_id={cid} and request_type='account_open'")[0]
post(senior, f"/approvals/{rid}", {"action": "review", "remarks": "ok"})
post(manager, f"/approvals/{rid}", {"action": "approve", "remarks": "ok"})
acc_no, acc_status = psql(f"select account_number || '|' || status from account where customer_id={cid}")[0].split("|")
check("Workflow over HTTPS: customer, KYC, maker-checker account opening", acc_status == "Active", f"{acc_no} {acc_status}")

sh("exec", "-T", "redis", "sh", "-c", 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHDB')  # reset sign-in throttle
tellers = [login(f"senior.{suffix}@bank.test", "Strong#Pass2026") for _ in range(8)]
hosts = []


def teller_deposit(t):
    html = t.get(f"{BASE}/transactions/new?account={acc_no}").text
    r = t.post(f"{BASE}/transactions/new?account={acc_no}",
               data={"_csrf": csrf(html), "_idem": idem(html), "txn_type": "Credit", "amount": "1000.50", "channel": "Cash"})
    hosts.append(r.headers.get("X-Instance"))


threads = [threading.Thread(target=teller_deposit, args=(tellers[i % 8],)) for i in range(16)]
[t.start() for t in threads]
[t.join() for t in threads]
bal = Decimal(psql(f"select balance from account where account_number='{acc_no}'")[0])
check("16 simultaneous deposits across both servers: balance exact", bal == Decimal("16008.00") and len(set(hosts)) == 2,
      f"{bal} via {set(hosts)}")
recon = sh("exec", "-T", "app1", "flask", "--app", "wsgi:app", "reconcile")
check("Books reconcile on the production stack", recon.returncode == 0, recon.stdout.strip().splitlines()[-1:] if recon.stdout else recon.stderr[-200:])

# ---- database role and audit integrity ------------------------------------------------
out, err = psql("delete from audit_log", user="bank_app", password=ENV["BANK_APP_DB_PASSWORD"])
check("Runtime DB role cannot delete audit history", "permission denied" in err, err[:80])
v = sh("exec", "-T", "app1", "flask", "--app", "wsgi:app", "verify-audit")
check("Audit hash chain verifies on the production stack", v.returncode == 0 and "'ok': True" in v.stdout, v.stdout.strip()[-120:])
logs = sh("logs", "--tail", "50", "app1").stdout
check("Structured JSON logs carry request ids and no query strings",
      '"request_id"' in logs and "pan=" not in logs.lower() and "?q=" not in logs, "")

failed = [t for t, ok in results if not ok]
print(f"\nproduction stack: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
