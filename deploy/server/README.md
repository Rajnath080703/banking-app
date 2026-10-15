# dev.bank.brunocode.in and main.bank.brunocode.in

Two environments on one server, from two branches of this repository:

| Branch | Site | Data | Purpose |
|---|---|---|---|
| `dev` | https://dev.bank.brunocode.in | sample customers (made up) | try every change here first |
| `main` | https://main.bank.brunocode.in | empty, for real use | release by merging `dev` → `main` |

Each environment has its own app, scheduler, PostgreSQL, Redis, secrets and admin account.
One Caddy proxy serves both names over HTTPS with automatic Let's Encrypt certificates.

## How changes flow

1. Commit to `dev` (or merge a pull request into `dev`) and push.
2. GitHub Actions runs all test suites; only if they pass, it deploys to dev.bank (backup →
   update → migrate → health check).
3. Check the change on https://dev.bank.brunocode.in.
4. Merge `dev` into `main` (pull request) and push: the same tests run, then main.bank is deployed.
   Optional: in GitHub → Settings → Environments → `main`, add yourself as a required reviewer so
   every production deploy waits for your click.

## One-time set-up

**1. Server** — Ubuntu 22.04/24.04, 2 vCPU / 4 GB RAM or more, ports 22, 80, 443 open.

**2. DNS (Cloudflare, zone brunocode.in)** — add two **A** records pointing at the server's IP:
`dev.bank` and `main.bank`. Set them to **DNS only (grey cloud)** so the app sees real client IPs
and Caddy can issue certificates. (If you later turn the orange-cloud proxy on, set Cloudflare
SSL mode to *Full (strict)* and tell me, because the trusted-proxy setting must change.)

**3. Run the set-up script on the server** (as root):

```
curl -fsSLO https://raw.githubusercontent.com/Rajnath080703/banking-app/main/deploy/server/setup.sh   # private repo: copy the file instead
sudo sh setup.sh git@github.com:Rajnath080703/banking-app.git <admin-email> <email-for-certificates>
```

The first run prints a **deploy key**: add it in GitHub → repo → Settings → Deploy keys (read-only),
then run the same command again. It then installs Docker, generates all secrets **on the server**
(`/opt/bank/dev.env`, `/opt/bank/main.env`, never in Git), starts the proxy and both environments,
loads sample data into dev only, and schedules daily database backups.

**4. Automatic deploys from GitHub** — create an SSH key pair for GitHub Actions, put the public key
in the server user's `~/.ssh/authorized_keys`, then add these repository secrets
(Settings → Secrets and variables → Actions): `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`
(private key), `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan <server>`). Until they exist, pushes
still run the tests and simply skip the deploy step.

## After the first deploy

- Sign in to each site with the admin email; the first password is `BANK_ADMIN_PASSWORD` in
  `/opt/bank/<env>.env`. **Change it immediately** and turn on two-factor authentication.
- Dev sample staff: `officer@demo.bank.test`, `senior@…`, `manager@…`, `clerk@…`; password is
  `BANK_DEMO_STAFF_PASSWORD` in `/opt/bank/dev.env`.
- Turn on *Enforce two-factor authentication* in Settings on **main**.

## Day to day (on the server)

```
sh /opt/bank/dev/deploy/server/deploy.sh dev        # redeploy by hand (also what GitHub runs)
docker compose -f /opt/bank/main/deploy/server/stack.yml --env-file /opt/bank/main.env -p bank-main logs -f app
docker compose -f /opt/bank/main/deploy/server/stack.yml --env-file /opt/bank/main.env -p bank-main exec app flask --app wsgi:app reconcile
ls /opt/bank/backups                                 # one before every deploy + one daily per environment
```

**Keep `/opt/bank/main.env` safe** (password manager or offline copy): its `BANK_DATA_KEY` decrypts
customer PAN, mobile and documents. Without it, backups of main cannot be read. Copy backups off
the server regularly.
