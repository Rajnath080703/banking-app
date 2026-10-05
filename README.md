# Local Bank — Customer 360 & Operations Platform

A secure banking app for employees, built with Python (Flask + SQLAlchemy). It has light and dark themes.

## Run

```
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:5000

| Account | Email | Password |
|---|---|---|
| **Super Admin** | rajnath080703@gmail.com | Bank@3006 |
| Demo staff (per branch) | manager / senior / officer / clerk `.main001@localbank.in` (also `.anna002`, `.cbe003`) | Demo@12345 |

The first start creates `instance/bank.db` with roles, the Super Admin and demo data.
Set `BANK_DEMO_DATA=0` to start with no demo data. To reset: delete `instance/bank.db` and run again.

## Modules
Dashboard (KPIs, charts, Today/Week/Month/Custom filters) · Branch Dashboard · Customer 360 ·
Accounts · Loans (Gold, Personal, Home, Vehicle, Business, Education, Other) · Deposits & FD ·
Transactions · KYC & Documents · Approvals (maker-checker) · Tickets · Reports (Excel/PDF export) ·
Alerts & fraud/risk rules · Audit Logs · User Management (roles × 16 modules × 7 actions) · Settings.

## Security
- RBAC with fine-grained permissions, checked on every protected route
- Staff see only their own branch's data
- Maker-checker: a maker can't check their own request, and a checker can't give final approval
- Optional TOTP MFA, which can be required for everyone
- Lockout after repeated failed logins, with an alert
- Session timeout on inactivity
- Password policy and expiry; passwords hashed with scrypt
- PAN, mobile and document numbers encrypted at rest (Fernet). Key is in `instance/data.key`, or set the `BANK_DATA_KEY` env var
- CSRF tokens, strict CSP and security headers
- Audit log of logins, profile views, data reveals, changes, approvals, exports and denied access

**Production:** run behind HTTPS with `BANK_HTTPS=1` (secure cookies + HSTS).
Optional env vars: `BANK_SSL_CERT` / `BANK_SSL_KEY`, `DATABASE_URL` (e.g. PostgreSQL), `BANK_SECRET_KEY`.
`bank/services.py` is the integration point for the core banking system. Replace its local database writes with calls to authenticated core banking APIs.
