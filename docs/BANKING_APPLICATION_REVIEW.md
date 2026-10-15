# Banking application review — Local Bank Customer 360 & Operations Platform

Review date: 9 October 2026 · Scope: the existing local application on a Windows workstation.
Nothing was deployed anywhere; no public tunnel was opened. All test and browser work used
synthetic data in separate databases. No secrets appear in this report.

Earlier rounds of work (8–9 October) are summarised where relevant; this round's findings are
marked **[R3]**.

---

## A. Executive summary

**Inspected:** the whole project — Flask views, services, templates and JavaScript, the SQLAlchemy
models and Alembic migrations, the PostgreSQL database actually configured in `.env`, the Docker
set-up, Git history, documentation, and all existing test suites. The application was started
locally and used through a real browser as an Officer, Senior Officer and Branch Manager.

**Most important findings this round**

1. **[R3, P1] The application's logger was silently disabled.** Running the database migrations at
   start-up (which `run.py` does) loaded Alembic's logging configuration, which switches off every
   existing logger. The real app wrote **0** request or error lines. Fixed; verified on the real
   running app (request lines now written, no query strings).
2. **[R3, P1] The configured database `bankdb` could not run the current code.** It still had the
   pre-migration schema (no migration history, missing columns). It held only reference data (1
   admin, 1 branch, 8 roles, 1 setting — no customers, accounts or transactions). Instead of
   dropping it, a new database `bankops` was built by the migrations, compared, and `.env` was
   pointed at it (backup of `.env` kept). `bankdb` is untouched.
3. **[R3, P1] No authorised way to correct a wrong posting.** A wrong deposit or withdrawal could
   only be offset by an unlinked opposite posting without approval. Added maker-checker
   **Transaction Reversal**.
4. **[R3, P2] No transfer between two accounts in the bank.** Added an atomic, deadlock-safe
   transfer with maker-checker above the high-value limit.
5. **[R3, P2] Account opening was not reachable from the Accounts page** and the shared customer
   picker accepted only an exact customer ID or account number. Added "Open Account" and a real,
   branch-scoped customer search used by five forms.
6. **[R3, P1] The hourly background scan took 393 s at 30,000 loans** (1M-row test) because each
   alert was its own savepoint round trip. Batched; the same run now takes 6.2 s. The large
   Loans Excel export (≈11–24 s for 30k rows) remains open.
7. **[R3, P0 — action needed by you] The Super Admin's current password is in Git history**
   (first commit, `README.md` and `bank/seed.py`) and the real admin account was created with it.
   It must be changed (section E). Old encryption keys committed in `instance/` were already
   rotated (verified: current keys differ).

**Remaining critical concerns:** the exposed admin password (until changed); no real deployment,
TLS, monitoring or scheduled off-site backups exist; no external penetration test; testing with
real employees has not happened.

---

## B. Application inventory

| Area | Implementation (verified in code) |
|---|---|
| Framework | Flask 3.1 app factory, blueprints, Jinja templates, vanilla JS; served locally by waitress (`run.py`), gunicorn in the container image |
| Database | PostgreSQL 16 (Docker container `bank-postgres`, bound to 127.0.0.1:5432); psycopg 3; SQLAlchemy 2.1; Alembic via Flask-Migrate (3 migrations) |
| DB roles | Owner role runs migrations only; runtime role `bank_app` has no DDL, cannot UPDATE/DELETE history tables (journal, audit, transactions except status/flag columns) |
| Money | `Decimal` everywhere (`bank/money.py`), NUMERIC(18,2) columns, half-up rounding to the paisa |
| Ledger | Double-entry (8 GL accounts); a deferred constraint trigger refuses unbalanced journal entries; `flask reconcile` compares ledger and product balances |
| Audit | SHA-256 hash-chained audit log, sealed rows immutable by trigger; `flask verify-audit` |
| AuthN | Passwords (12+ chars, policy checks), invite/reset one-time links (hashed), TOTP MFA with replay protection, lockout, rate limits, idle (15 min) and absolute (12 h) sessions |
| AuthZ | 11 roles × 16 modules × 8 actions, checked on the server (`require_perm`); branch scoping (`branch_scope`, `ensure_branch_access`); maker-checker for 14 request types |
| Data protection | Fernet encryption of PAN, mobile, document numbers, uploaded files, MFA secrets, approval payloads; HMAC search hashes; CSRF + one-time form tokens; strict CSP |
| Background work | In-process scan (or `flask scan`): overdue loans, penal state, KYC expiry, dormancy, FD maturity payout, audit sealing — advisory-locked across processes |
| Reports | 14 reports, Excel/PDF export with permission, row cap, rate limit and formula-injection protection |

---

## C. Confirmed findings

Priorities: P0 critical · P1 high · P2 medium · P3 low. "Confirmed" means reproduced with evidence.

| # | Pri | Module | Finding | Evidence | Fix | Verified by |
|---|---|---|---|---|---|---|
| 1 | P1 | Logging | App logger disabled after migrations; no request/error logs in the real app | Logger `bank` had `disabled=True`; real app log had 0 request lines | `migrations/env.py`: `fileConfig(..., disable_existing_loggers=False)`; `logging_setup` re-enables the app logger | `test_security_isolation` "Request logging is active" (4 records); real app restarted: request lines present, query string absent |
| 2 | P1 | Database | Configured DB on pre-migration schema; current code cannot run on it | No `alembic_version`, 22 tables, no `mfa_last_step` | New `bankops` built by migrations; reference data compared; `.env` switched; backups of old DB verified (22 tables) | `/readyz` = ready (database ok, schema ok); `flask db check`: no drift |
| 3 | P1 | Transactions | No authorised reversal of a wrong posting | Code inspection; no route or service | `reversal_problem`, `_exec_txn_reversal`, `POST /transactions/<id>/reverse`, maker-checker | `test_operations` (11 reversal checks incl. maker cannot approve, once only, blocked if money gone, books reconcile); browser walkthrough |
| 4 | P2 | Transactions | No transfer between two accounts | Code inspection: "Transfer" only labels a one-sided clearing posting | `transfer_funds` (row locks in id order, both legs vs clearing), `/transactions/transfer`, `fund_transfer` approval | `test_operations`: exact amounts, double-submit once, 8 opposite concurrent transfers (no deadlock, totals unchanged), two simultaneous approvals execute once, books reconcile |
| 5 | P2 | Accounts | No "Open Account" on the Accounts page; picker exact-ID only | Browser: no action; `/accounts/new?ref=<name>` → "No customer found" | Permission-aware button; picker searches name/mobile/PAN/ID/account in branch scope, shows KYC; form shows existing accounts and rules | `test_operations` (picker by name/mobile/PAN, other branch hidden and refused by id, Clerk sees no button and gets 403); browser walkthrough |
| 6 | P2 | Forms | Transaction and FD forms lost typed values on error | Redirect after flash | Re-render with entered values | `test_operations` (both forms) |
| 7 | P3 | Accounts | No Frozen filter tab | Template | Added | `test_operations` |
| 8 | P3 | Picker | Short name search suggested registering a new customer | Screenshot | Asks for 3 letters | `test_operations` |
| 9 | P1 | Test fixture | Hard-coded admin password fallback in `tests/harness.py` | grep | Removed; tests require the value from the environment | Full suite run |
| 10 | P2 | Docs | README out of date (6 roles, old DB name, missing settings, no tests) | Comparison with code | README rewritten; manual updated | Commands in README executed (section M) |
| 12 | P1 | Background scan | First scan over 30,000 loans took 393 s (blocking the scan lock and a DB connection for 6.5 min every hour) | 1M-row run; profile: 55.5 of 59.5 s in per-alert savepoints | Scan collects alerts and inserts them in batches with `ON CONFLICT DO NOTHING` on the same partial unique index | Same run after fix: 6.2 s; workflows suite (no duplicate alerts) |
| 13 | P2 | Reports | Loans report export of 30k rows takes ≈11–24 s | 1M-row run; profile: time is in openpyxl XML writing, not the database | **Not fixed.** openpyxl write-only mode was tried and measured with no gain, so it was reverted. Options: CSV export for large data, a faster writer library, or background export jobs | — |
| 11 | P2 | Test fixture | 1M-row performance test broken (missing `status` column; bulk load hits the app role's statement timeout) | Test output | Fixture inserts `status`; bulk load over the owner connection | Performance run (section I) |

**Fixed in earlier rounds and re-verified this round (still passing):** lost updates on concurrent
deposits, duplicate loan disbursement on simultaneous approvals, NaN amounts, overpayments, FD
maturity payout, rounding, double submissions, partial failures (all inside one DB transaction),
login enumeration, MFA plaintext and replay, X-Forwarded-For spoofing, cross-branch settings edit,
spreadsheet formula injection, KYC renewal, open redirect on KYC decisions, account opening
without KYC, dashboard deposit trend (pending FDs), Western digit grouping in messages.

**Potential risks (not confirmed defects):**
- PAN or mobile typed into the search box travels in the URL query string (GET). It is excluded
  from the app's logs (verified) and audit log (verified), but stays in browser history.
- Inter-branch transfers are not supported (both accounts must be in the employee's branch scope).
- Transfers are not reversible through the reversal screen; a wrong transfer is undone by a
  transfer back.
- Dashboard "income vs outgoing" and the transaction reports show *gross* flow: internal
  transfers, loan disbursals, FD bookings and EMI debits all count. Net flow and the deposits trend
  are unaffected (a transfer adds the same amount to both sides), but a manager reading "income"
  as new money would over-read it. Consider showing internal movements separately.

---

## D. Employee UX review

See `docs/EMPLOYEE_WORKFLOW_IMPROVEMENTS.md` for the full tables. Summary:

- **Workflows exercised in a real browser:** sign-in, dashboard, customer search, Customer 360,
  cash deposit, EMI collection, checker review, manager approval, KYC verification, FD booking,
  account opening (two routes), transfer, reversal request and approval, report export, menu work
  counts. Light and dark themes and a 390 px phone width were checked on the changed screens.
- **Measured round 1:** account-number lookup 2→1 clicks; deposit 5→4; EMI 5→4; each further
  approval or KYC item 4→2; FD of ₹20,250 refused→4 clicks.
- **Measured round 2:** opening an account from the Accounts page went from *not possible* to
  6 clicks; the Customer 360 route (5 clicks) is unchanged. Transfer 5 clicks; reversal request 4.
- **Not done:** sessions with real employees; screen-reader and automated accessibility audits.

---

## E. Security review

**Tests performed this round (`tests/test_security_isolation.py`, 17 checks):**
- Branch-1 Officer, Senior Officer and Manager tried 16 record-id and reference URLs of a
  branch-2 customer: none returned branch-2 data.
- 10 direct POSTs to branch-2 records: all 403, and no record changed.
- Branch-2 records never appeared in branch-1 lists, search or reports. The Super Admin does see
  them.
- CSRF: requests with no token, a forged token, or another session's token were all 400, and the
  balance was unchanged. Sign-out without a token is refused.
- XSS: script in a customer name, address and ticket was escaped on 9 pages; the CSP has no
  `unsafe-inline` for scripts.
- SQL injection: 4 payloads × 9 search, sort, filter and account parameters gave no 5xx and no
  data leakage.
- 10 simultaneous ₹1,000 withdrawals against ₹5,000: exactly 5 succeeded, the balance ended at
  ₹0, and the books reconcile.
- Request logs are active and contain no PAN, mobile or password. PAN and mobile are encrypted in
  the database, and the audit log omits search terms.
- Security headers are present, with `Cache-Control: no-store`. An unknown record gives a plain
  404 with no stack trace.

**Also run:** the role × page × action permission sweep (284 checks), rate-limit tests,
`bandit` (0 issues in 5,427 lines) and `pip-audit` (no known vulnerabilities).

**Credentials and keys**

| Item | Status | Action |
|---|---|---|
| Super Admin password | In Git history (first commit) and was used to create the admin in `bankops` | **Change it now**: sign in → menu under your name → Change password (or `flask --app wsgi:app reset-admin-password`). Then put a new value in `.env` `BANK_ADMIN_PASSWORD` (only used when the account is first created). |
| `instance/data.key`, `instance/secret.key`, `instance/bank.db` | In Git history (first commit) | Current keys differ (verified), so the committed keys protect only the old demo SQLite data (90 synthetic customers, 14 employee hashes). Purge the history (e.g. `git filter-repo`) before the repository is ever shared or made public; the GitHub remote currently answers "repository not found". |
| `BANK_DATA_KEY` (current) | Not exposed in Git | Do **not** replace casually: it decrypts PAN, mobile, document numbers, uploaded files, MFA secrets and approval payloads. Rotation needs a planned re-encryption with old and new keys, after a verified backup. |
| `BANK_SECRET_KEY`, DB passwords (current) | Not in Git | Rotating `BANK_SECRET_KEY` only signs everyone out. DB passwords: change in PostgreSQL and `.env` together. |

**Unresolved:** no external penetration test; MFA is optional by default (`enforce_mfa` = off);
local HTTP (no TLS) means cookies are not `Secure` locally; search terms in browser history.

---

## F. Banking correctness

- **Ledger:** every posting is a balanced journal entry, enforced by a database trigger. Total
  debits equal total credits after every workflow test. The reconciliation checks GL 2000 against
  account balances, GL 1200 against loan principal, GL 2100 against open FDs, and each account's
  own ledger against its balance. All passed after deposits, withdrawals, transfers (including
  concurrent ones), reversals, loans, FDs, interest and rejections.
- **Calculations checked against hand-computed values** (`test_hardening`):
  - EMI on ₹1,00,000 at 12% for 12 months = ₹8,884.88; ₹2,00,000 at 9.5% for 12 months = ₹17,536.70
  - FD of ₹50,000 at 6.8% for 12 months, quarterly compounding = ₹53,487.69
  - Interest on ₹1,00,000 at 12% for 30 days = ₹986.30; interest for 40 days = ₹1,315.07
  - Penal charge, EMI × 24% × 10/365 = ₹58.42
  - Savings interest = Σ daily balance × 3%/365
  - Half-up rounding: ₹2.675 → ₹2.68
  - Early FD closure at the rate minus 1%; payment allocation penal → interest → principal
- **Concurrency:**
  - 12 simultaneous deposits and 10 simultaneous withdrawals: exact results.
  - 8 opposite transfers at once: no deadlock.
  - Two managers approving the same loan, or the same transfer, at the same moment: executes once.
  - Double-submitted forms execute once.
- **Integrity:** each financial operation runs in one database transaction with row locks. An
  approval re-checks its conditions under a lock (reversal, transfer, account opening with KYC).
  History is append-only for the runtime role.
- **Not verified:** fees and charges (no fee engine exists); tax deducted at source on interest
  (not implemented); asset classification of non-performing loans beyond days overdue (not
  implemented).

---

## G. Database review

- **Engine in use:** PostgreSQL 16.x in Docker (`bank-postgres`), port bound to 127.0.0.1 only.
  Connectivity verified by `/readyz` and by every test suite.
- **Migrations:** 3 Alembic revisions. Every test suite builds a fresh database through them.
  `flask db check` reports no drift between the models and the migrations. The old `bankdb`
  cannot be upgraded in place, because it predates the migrations, so a new database was built
  instead (finding 2).
- **Constraints reviewed:**
  - CHECK constraints on amounts, balances, statuses, rates and tenures
  - unique business numbers
  - foreign keys
  - a partial unique index on open alerts
  - a trigger refusing unbalanced journals; no-update and no-delete triggers on history tables
  - a trigram index for name search
  - composite indexes on transactions
- **Data preservation:**
  - `bankdb` was backed up twice and both files were verified readable (22 tables each); it was
    left untouched.
  - `.env` was backed up as `.env.bak-20261009` (git-ignored).
  - A backup of `bankops` was restored into a scratch database: reconciliation and audit
    verification passed, and the scratch database was then removed.
- **Demo data:** none in the operational database (`bankops`: 0 customers, 0 accounts, 0
  transactions; only reference data). The old SQLite demo database exists only in Git history.
  Test fixtures live in throwaway `bankdb_t_*` databases that are dropped after each run.
  `bankdb_ux` holds the synthetic walkthrough data and can be dropped when no longer needed.

---

## H. Testing results

Final run: `python tests/run_all.py` on 9 October 2026 (all suites on throwaway databases).
Numbers are copied from the run output.

| Test suite | Passed | Failed | Skipped | Status | Important findings |
|---|---|---|---|---|---|
| Database integrity (constraints, triggers, privileges) | 15 | 0 | 0 | Ran | Unbalanced journals refused; history immutable for the app role |
| Regression: 26 audit defects | 26 | 0 | 0 | Ran | All 26 earlier defects stay fixed |
| Hardening: ledger, calculations, security | 60 | 0 | 0 | Ran | Hand-computed EMI, FD, interest, penal, rounding |
| Permission sweep (9 roles × pages × actions) | 284 | 0 | 0 | Ran | No mismatch between granted rights and server behaviour |
| End-to-end employee journey | 7 | 0 | 0 | Ran | See note below |
| Dashboard figures vs SQL | 7 KPIs + trend | 0 | 0 | Ran | KPIs match independent SQL |
| Workflows (tickets, alerts, scan, rejections, admin) | 27 | 0 | 0 | Ran | Scan with batched alerts: no duplicates |
| Employee workflow improvements | 25 | 0 | 0 | Ran | Queues, KYC gate, direct search hits |
| **Security & branch isolation (new)** | 17 | 0 | 0 | Ran | Found the disabled logger (fixed) |
| **Operations: open account, transfers, reversals (new)** | 41 | 0 | 0 | Ran | New features incl. concurrency |
| Rate limits | 4 | 0 | 0 | Ran | Sign-in and export throttling |
| **Total** | **11/11 suites** | 0 | 0 | | Line coverage of `bank/`: **88%** |

**Note on one failed run.** The first final run (same code) failed the end-to-end suite: it took
947 s instead of ~35 s, every suite ran 2–3× slower, and the admin's session hit the 15-minute idle
limit mid-test (pages returned 302 to sign-in — the timeout working as designed). Run alone it
passed 7/7 in 37 s; the full repeat passed 11/11. Other desktop applications were running at the
time. This is recorded as an environment-load effect, not proven beyond that evidence.

Also run: `tests/perf_million_rows.py` and `tests/perf_scan.py` (section I).

The production-like Docker stack test (`tests/test_prod_stack.py`, 16/16 on 8 October) was **not
re-run this round**: the stack was stopped to free memory.

---

## I. Code quality and performance

- `ruff` (F, E9, B, S rules) on `bank/`: all checks passed. `bandit -r bank`: 0 issues
  (5,427 lines). `pip-audit -r requirements.txt`: no known vulnerabilities. All 56 templates
  compile; the app registers 70 routes.
- Coverage across all suites: see the TOTAL line in section H.

**Performance — measured, single local process, one PostgreSQL in Docker, Windows workstation.**
Data set: 50,000 customers, 60,000 accounts, 1,000,000 transactions, 30,000 loans
(`tests/perf_million_rows.py`). Timings vary noticeably with other load on this machine (the same
export ranged 6.6–24.9 s across runs), so treat single numbers as indicative.

| Page / job | Before this round | After this round | Note |
|---|---|---|---|
| Hourly background scan (30k loans, first run creating alerts) | **393,057 ms** | **6,177 ms** | Fixed: alerts inserted in batches (finding 12) |
| Dashboard (month / year) | 148 / 279 ms | 109 / 117 ms | No code change; machine quieter in second run |
| Transactions list (month) | 431 ms | 215 ms | — |
| Customer search (name / exact ID) | 655 / 358 ms | 220 / 165 ms | — |
| Accounts sorted by balance | 501 ms | 238 ms | — |
| Loans list / overdue | 818 / 520 ms | 356 / 283 ms | — |
| New: Open Account picker (name / ID) | 951 / 573 ms | 138 / 127 ms | — |
| New: transfer screen | 204 ms | 57 ms | — |
| Loans report on screen | 5,726 ms | 2,217 ms | Open issue (finding 13) |
| Loans report Excel export (30k rows) | 24,011 ms | 10,857 ms | Open issue (finding 13) |

Profiling (`tests/perf_scan.py`, 6,000 loans): first scan 59.5 s, of which 55.5 s in per-alert
savepoints (≈7,200 round trips); after the fix 3.3–9.9 s over three runs, with alert insertion
1.3 s.

---

## J. Changed files (this round)

| File | Change | Why | Verified by |
|---|---|---|---|
| `migrations/env.py`, `bank/logging_setup.py` | Keep the app logger enabled when migrations run | Logs were silently off | Security suite; real app log |
| `bank/services.py`, `bank/models.py` | Reversal eligibility + handler; transfer service + handler; request labels | Missing banking controls | Operations suite |
| `bank/views/transactions.py` | `POST /transactions/<id>/reverse`; `/transactions/transfer`; form keeps values | As above | Operations suite, browser |
| `bank/views/deposits.py`, `templates/deposits/new.html` | FD form keeps values | Re-entry after errors | Operations suite |
| `bank/security.py`, `templates/_customer_picker.html`, `bank/__init__.py` | Branch-scoped customer search in the shared picker; small template filter | Exact-ID-only picker | Operations suite, browser |
| `templates/accounts/index.html`, `accounts/new.html` | Open Account button; Frozen tab; existing accounts, rules, change-customer | Account-opening friction | Operations suite, browser |
| `templates/transactions/index.html`, `customers/profile.html`, `macros.html`, `approvals/detail.html`, `transactions/transfer.html` (new) | Reverse button, status badges, transfer entry points, approval link | UI for the new controls | Operations suite, screenshots (light/dark) |
| `tests/test_security_isolation.py`, `tests/test_operations.py` (new) | 17 + 41 checks | Coverage gaps (XSS, CSRF variants, isolation by id, withdrawal races, logs; new features) | Run results |
| `tests/harness.py`, `tests/perf_million_rows.py`, `tests/run_all.py` | No hard-coded password; perf fixture fixed; suites registered | Secret hygiene; broken fixture | Run results |
| `README.md`, `docs/*`, `user guide/*.pdf` | Accurate run/test/backup instructions; this review; manual updated | Documentation was out of date | Commands executed |
| `.env` (local, git-ignored) | Database name `bankdb` → `bankops` (backup kept) | Old schema | `/readyz` |
| `bank/services.py` (scan) | Batched alert inserts in the scheduled scan | 393 s scan | Perf runs, workflows suite |
| `tests/perf_scan.py` (new) | Profiles the scan and loans report on N loans | Evidence for finding 12/13 | Output in section I |
| `tests/run_all.py` | Summary line skips JSON log lines | Logs now print (after finding 1) | Final run |
| `.gitignore` | Ignore coverage files | Hygiene | — |

---

## K. Scores (out of 100, evidence-based; provisional where stated)

| Area | Score | Evidence and what holds it back |
|---|---|---|
| Security | **80** | Strong server-side controls verified by 17 + 284 + 60 checks, scanners clean. Held back by the exposed admin password still active, no external pen test, MFA optional, no TLS locally. |
| Functionality | **84** | All core workflows exercised end to end, including new transfers and reversals; books reconcile. Missing: fees/charges, TDS, inter-branch transfers, standing instructions, cheque clearing. |
| UI/UX | **78** (provisional) | Measured improvements on 15 tasks; both themes and phone width checked. No real-user testing or accessibility audit. |
| Performance | **72** | Common pages stay under ~0.4 s at 1M transactions; the scan bottleneck was found and fixed (393 s → 6 s). Large exports are still slow (Loans Excel ≈11–24 s for 30k rows); no concurrent-user load test. |
| Scalability | **68** (provisional) | Two app servers behind a proxy with shared Redis rate limits passed earlier (8 Oct); advisory-locked scan. No load test with concurrent users at volume; single local process today. |
| Code quality | **82** | Lint/static analysis clean, 88% coverage, focused modules. Tests are scripts rather than a pytest suite; some large view functions. |
| Production readiness | **50** | Not deployed. Missing: TLS, monitoring/alerting, scheduled off-site backups, disaster recovery plan, SMTP, key management, operational runbooks, user acceptance testing. |

---

## L. Remaining work

**Critical blockers (before any real use):**
- Change the Super Admin password.
- Purge `instance/` keys and the old database from Git history before the repository is shared.
- Enable `enforce_mfa`.

**High priority:**
- Real TLS.
- Scheduled, off-machine and encrypted backups, with a restore drill.
- Monitoring of the JSON logs, plus alerting on errors and reconciliation failures.
- A documented key-rotation procedure with re-encryption.
- User acceptance testing with branch staff.

**Medium:**
- Fees and charges engine; TDS on interest.
- Inter-branch transfers (head-office approval).
- Move search to POST, or clear history, for PAN/mobile searches.
- Receipts after a transaction or transfer.
- Branch Actions placement; queue type shortcuts.

**Optional:**
- Convert the script tests to pytest.
- Accessibility audit (axe and a screen reader).
- Drop `bankdb_ux` and the old `bankdb` once no longer needed (after a final backup).

**Future cloud deployment (out of scope here):**
- Managed PostgreSQL with point-in-time recovery.
- A secrets manager.
- Web application firewall and TLS termination.
- Centralised logging; a vulnerability-scanning pipeline.
- Penetration test; data-residency review.

---

## M. How to run locally (commands verified this round)

```
docker compose up -d                       # database (127.0.0.1:5432)
python run.py                              # migrations + app at http://127.0.0.1:5000
curl http://127.0.0.1:5000/readyz          # status check: {"status": "ready", ...}
python tests/run_all.py                    # all suites on throwaway databases
python tests/run_all.py --perf             # plus the 1M-transaction performance run
flask --app wsgi:app reconcile             # books check on the live database
flask --app wsgi:app verify-audit          # audit hash-chain check
docker exec bank-postgres pg_dump -U <POSTGRES_USER> -d bankops --format=custom > backups/bankops_YYYYMMDD.dump
# stop: Ctrl+C in the run.py terminal, then: docker compose stop
```

---

## N. Final verification status

- **Implemented this round:** logging fix; database switched to the migrated `bankops`;
  transaction reversal; transfers between accounts; Open Account from the Accounts page;
  searchable customer picker; forms keep typed values; Frozen filter; README, manual and these
  reports; test fixtures fixed.
- **Actually tested:** see section H (11 suites, 513 individual checks), plus browser walkthroughs (round 2: 9 tasks)
  and screenshots of the changed screens in light and dark mode. Real app started:
  `/readyz` ready, login page served, request logging verified. Backup and restore verified.
- **Passed / failed:** see section H. The earlier production-stack suite was not re-run.
- **Not tested:** real employees; accessibility tools; TLS; SMTP; deployment; external
  penetration test; behaviour beyond the 1M-row data set.
- **Needs human review:** the admin password change; whether Officers may collect EMIs; whether
  Clerks should be able to make transfers (they can today, because they hold the transactions
  "create" right); the decision to keep or drop the old `bankdb`.
- **Local application:** started successfully on `bankops` and exercised. No business data was
  created in it.

---

## Addendum — account-opening workflow (9 October 2026)

**Root cause (confirmed against the last commit):** the committed Accounts page contained no link to
the account form, and the form's customer picker accepted only an exact customer ID or account
number, so employees had to go through Customer 360. Fixed in round 3 (Open Account button,
searchable picker). Re-inspection against the full requirement list found three further gaps,
all reproduced before fixing:

| Gap | Reproduction | Fix | Test |
|---|---|---|---|
| Two pending requests of the same type for one customer were accepted | 2 pending Savings accounts created | Shared rule `account_open_problem()` refuses while one is waiting, naming the request; the form shows waiting requests up front | `test_open_account.py` |
| Simultaneous submissions could all pass that check | With the check window widened in the test: 6 of 6 got through without a lock | The customer row is locked (`SELECT … FOR UPDATE`) during the check; result 1 of 6 | same (fails without the lock, passes with it) |
| Accounts could be requested for a non-active customer record | Request accepted for an Inactive customer | Same shared rule, checked at request and again at final approval | same |

Also: validation errors re-show the form with the reason and the chosen type (no redirect); the
review dialog names type, customer, customer ID and branch; the success message states the
account number and that it is pending approval.

**Tests run:** `test_open_account.py` 27/27 (both entry points, 6 roles' permissions incl. direct
POSTs, search by 5 identifiers, branch isolation, validation, KYC and status eligibility, double
submission, duplicate pending request, 6-way race, maker cannot approve, KYC lapse at approval,
approve, reject then re-request, audit and timeline entries). Full suite: **12/12 suites passed,
88% coverage** (run under heavy machine load from other applications — suites took up to 15×
their usual time but all passed). Browser (Edge): 9/9 checks on both routes, including a
double-click on the confirm button (one request in the database), the duplicate-request message,
the KYC block, checker + manager approval and the Clerk's missing button.

**Not added:** extra account fields (nominee, mode of operation, initial deposit). The account
model has none; adding them would need new business rules and a migration, so it is left as a
product decision.
