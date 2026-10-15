#!/bin/sh
# One-time set-up of a fresh Ubuntu 22.04/24.04 server for both environments. Run as root:
#   sudo sh setup.sh <repo-ssh-url> <admin-email> <acme-email>
# e.g. sudo sh setup.sh git@github.com:Rajnath080703/banking-app.git you@example.com you@example.com
# The repository is private: add this server's deploy key (printed below) to GitHub first
# (repo → Settings → Deploy keys → Add, read-only), then run the script again.
set -eu
REPO="$1"; ADMIN_EMAIL="$2"; ACME_EMAIL="$3"
BASE=/opt/bank

# Docker and firewall
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if command -v ufw >/dev/null; then
  ufw allow OpenSSH; ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 443/udp; ufw --force enable
fi
mkdir -p "$BASE/backups"; chmod 700 "$BASE"

# Read-only deploy key for the private repository
if [ ! -f /root/.ssh/bank_deploy ]; then
  ssh-keygen -t ed25519 -N "" -C "bank-server-deploy" -f /root/.ssh/bank_deploy
  printf 'Host github.com\n  IdentityFile /root/.ssh/bank_deploy\n  IdentitiesOnly yes\n' >> /root/.ssh/config
  ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null
  echo "Add this key to GitHub (repo → Settings → Deploy keys, read-only), then re-run:"; cat /root/.ssh/bank_deploy.pub; exit 0
fi

secret() { openssl rand -hex 24; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

for ENV in dev main; do
  [ -d "$BASE/$ENV/.git" ] || git clone --branch "$ENV" "$REPO" "$BASE/$ENV"
  if [ ! -f "$BASE/$ENV.env" ]; then           # secrets are generated here and never leave the server
    ADMIN_PW="Adm#$(openssl rand -hex 8)"
    {
      echo "ENV_NAME=$ENV"
      echo "DB_OWNER_PASSWORD=$(secret)"
      echo "BANK_APP_DB_PASSWORD=$(secret)"
      echo "REDIS_PASSWORD=$(secret)"
      echo "BANK_SECRET_KEY=$(openssl rand -hex 32)"
      echo "BANK_DATA_KEY=$(fernet)"
      echo "BANK_ADMIN_EMAIL=$ADMIN_EMAIL"
      echo "BANK_ADMIN_PASSWORD=$ADMIN_PW"
      if [ "$ENV" = dev ]; then
        echo "BANK_DEMO_DATA_ALLOWED=1"
        echo "BANK_DEMO_STAFF_PASSWORD=Demo#$(openssl rand -hex 8)"
      else
        echo "BANK_DEMO_DATA_ALLOWED=0"
      fi
    } > "$BASE/$ENV.env"
    chmod 600 "$BASE/$ENV.env"
    echo "$ENV: first-sign-in admin password stored in $BASE/$ENV.env (BANK_ADMIN_PASSWORD) — change it after signing in."
  fi
done

# Shared proxy (ports 80/443) for both environments
docker network inspect bank-edge >/dev/null 2>&1 || docker network create bank-edge
printf 'ACME_EMAIL=%s\nDEV_HOST=dev.bank.brunocode.in\nMAIN_HOST=main.bank.brunocode.in\nCADDYFILE=%s\n' \
  "$ACME_EMAIL" "$BASE/main/deploy/server/Caddyfile" > "$BASE/proxy.env"
docker compose -f "$BASE/main/deploy/server/proxy.yml" --env-file "$BASE/proxy.env" -p bank-proxy up -d

# Both environments, then sample data on dev only
sh "$BASE/dev/deploy/server/deploy.sh" dev
sh "$BASE/main/deploy/server/deploy.sh" main
docker compose -f "$BASE/dev/deploy/server/stack.yml" --env-file "$BASE/dev.env" -p bank-dev \
  exec -T scheduler flask --app wsgi:app seed-demo || echo "(sample data not loaded — already present?)"

# Daily backups at 01:30 (kept 30 days by deploy.sh's rotation as well)
cat > /etc/cron.d/bank-backup <<CRON
30 1 * * * root for e in dev main; do docker compose -f $BASE/\$e/deploy/server/stack.yml --env-file $BASE/\$e.env -p bank-\$e exec -T db pg_dump -U bankowner -d bankdb --format=custom > $BASE/backups/bank-\$e-daily_\$(date +\\%Y\\%m\\%d).dump; done; find $BASE/backups -name '*-daily_*.dump' -mtime +30 -delete
CRON
echo "Done. https://dev.bank.brunocode.in and https://main.bank.brunocode.in"
