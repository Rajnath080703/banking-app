"""Rate limits are enforced server-side: sign-in (per client IP) and report exports (per employee)."""
import os
import sys

os.environ["BANK_TEST_RATELIMIT"] = "1"
os.environ.setdefault("BANK_TEST_DB", "bankdb_rl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ADMIN, app, cleanup, form_token  # noqa: E402

results = []
try:
    c = app.test_client()
    codes = []
    for _ in range(12):
        t = form_token(c, "/login")[0]
        codes.append(c.post("/login", data={"_csrf": t, "email": "guess@bank.test", "password": "wrong"}).status_code)
    results.append(("11th sign-in attempt within a minute is throttled (429)", 429 in codes and codes[:10].count(429) == 0, codes))
    other_ip = app.test_client()
    t = form_token(other_ip, "/login")[0]
    r = other_ip.post("/login", data={"_csrf": t, "email": ADMIN[0], "password": ADMIN[1]},
                      environ_base={"REMOTE_ADDR": "10.9.9.9"})
    results.append(("A different client IP is not affected", r.status_code == 302, r.status_code))
    exports = [other_ip.get("/reports/loans?export=xlsx", environ_base={"REMOTE_ADDR": "10.9.9.9"}).status_code for _ in range(32)]
    results.append(("31st report export within an hour is throttled", exports[:30].count(429) == 0 and exports[-1] == 429,
                    exports[-3:]))
    results.append(("Health probes are never rate limited", all(c.get("/healthz").status_code == 200 for _ in range(30))))
finally:
    cleanup()
for title, ok, *detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {title} :: {detail[0] if detail else ''}")
print(f"rate limits: {sum(1 for r in results if r[1])}/{len(results)} passed")
sys.exit(0 if all(r[1] for r in results) else 1)
