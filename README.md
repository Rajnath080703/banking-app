# Local Bank — Customer 360 & Operations Platform

An internal operations application for bank employees (not customers), built with Python
(Flask, SQLAlchemy, Alembic) on **PostgreSQL**, with light and dark themes. It runs locally;
nothing here is deployed to the internet.

- **Employee guide:** `user guide/Local Bank Operations - User Manual.pdf`
- **Engineering review, test results and open items:** `docs/BANKING_APPLICATION_REVIEW.md`

## Run locally (Windows)

```
pip install -r requirements.txt
docker compose up -d          # starts PostgreSQL on 127.0.0.1:5432 (first time, and after a reboot)
python run.py                 # applies database migrations, then serves http://127.0.0.1:5000
```

Check it is up: open http://127.0.0.1:5000/readyz — it reports `"status": "ready"` when the
database is reachable and the schema is at the latest migration.

Stop it with **Ctrl+C** in the terminal running `run.py`; stop the database with `docker compose stop`.

Sign in as the Super Admin (the email in `BANK_ADMIN_EMAIL`). The password is set from
`BANK_ADMIN_PASSWORD` only when the account is first created; change it after the first sign-in
(**Change password** in the menu under your name) or with `flask --app wsgi:app reset-admin-password`.

## What the first start creates

`run.py` brings the database up to date with the migrations in `migrations/` (as the owner role)
and then ensures the reference data. It never creates demo customers, accounts or transactions:

- the 11 system roles and their permissions
- the default fraud/risk rules and settings
- one branch, "Main Branch" (code MAIN001)
- the chart of accounts for the double-entry ledger
- the Super Admin account

Then: **Settings → Branches** (rename and add branches), **User Management → Add employee**
(staff receive a one-time invite link to set their own password), **Customer 360 → New customer**.

## Configuration (`.env`)

All settings and secrets live in `.env` in the project folder. It is git-ignored and must never
be committed, emailed or copied to shared folders.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Runtime connection as the restricted role `bank_app` (no DDL, append-only history) |
| `BANK_DB_OWNER_URL` | Owner connection, used only to run migrations at start-up |
| `BANK_APP_DB_PASSWORD` | Password the start-up assigns to the `bank_app` role |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Used by `docker-compose.yml` to create the database server |
| `BANK_SECRET_KEY` | Signs sessions and form tokens |
| `BANK_DATA_KEY` | Encrypts PAN, mobile and document numbers, uploaded files, MFA secrets and approval payloads. **Back it up securely: if it is lost or changed, that data cannot be decrypted.** Never replace it without a planned re-encryption. |
| `BANK_ADMIN_EMAIL` / `BANK_ADMIN_PASSWORD` | Super Admin created on first start |
| `BANK_HTTPS=1` | Optional. Secure `__Host-` cookies and HSTS when served over HTTPS |
| `REDIS_URL` | Optional. Shared rate limits and cache when more than one app process runs |

## Back up and restore the local database

```
docker exec bank-postgres pg_dump -U <POSTGRES_USER> -d <database> --format=custom > backups/bank_YYYYMMDD.dump
docker exec -i bank-postgres pg_restore --list < backups/bank_YYYYMMDD.dump     # verify the file is readable
```

The database name is the last part of `DATABASE_URL`. Restore into a **new** database, never over
the live one, then run `flask --app wsgi:app reconcile` and `flask --app wsgi:app verify-audit`
against it. `backups/` is git-ignored.

## Tests

```
python tests/run_all.py            # 11 suites, each on its own throwaway database, with coverage
python tests/run_all.py --perf     # also the 1,000,000-transaction performance run (several minutes)
```

The suites never touch the database in `DATABASE_URL`: each creates and drops its own
`bankdb_t_*` database through the real migrations and connects as the restricted role.

## Modules

Dashboard · Branch Dashboard · Customer 360 · Accounts (open, freeze, close) · Transactions
(deposits, withdrawals, transfers between accounts, authorised reversals) · Loans (Gold, Personal,
Home, Vehicle, Business, Education, Other) · Deposits & FD · KYC & Documents · Approvals
(maker-checker) · Tickets · Reports (Excel/PDF) · Alerts & risk rules · Accounting (ledger, trial
balance, reconciliation, savings interest) · Audit Logs · User Management · Settings.

## Security in brief

- Permissions checked on the server for every protected route; staff see only their own branch
- Maker-checker for sensitive changes; nobody approves their own request
- Double-entry ledger enforced by the database; history tables are append-only
- Tamper-evident (hash-chained) audit log
- TOTP MFA with replay protection, lockout, rate limits, idle and absolute session limits
- CSRF tokens, one-time form tokens, strict CSP, encrypted customer identifiers and documents
