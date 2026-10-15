#!/bin/sh
# Restore a backup into a NEW database (never over the live one), then verify it.
#   deploy/restore.sh backups/bankdb_....dump [bankdb_restore]
set -eu
cd "$(dirname "$0")/.."
dump="$1"; target="${2:-bankdb_restore}"
c="docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod"
app_pw=$(grep '^BANK_APP_DB_PASSWORD=' deploy/.env.prod | cut -d= -f2-)
$c exec -T db psql -U bankowner -d postgres -v ON_ERROR_STOP=1 -q \
  -c "DROP DATABASE IF EXISTS $target WITH (FORCE)" -c "CREATE DATABASE $target"
$c exec -T db pg_restore -U bankowner -d "$target" --exit-on-error < "$dump"
$c exec -T db psql -U bankowner -d "$target" -q -c "GRANT CONNECT ON DATABASE $target TO bank_app"
echo "restored into $target; verifying ledger reconciliation and audit chain..."
url="postgresql+psycopg://bank_app:${app_pw}@db:5432/${target}"
$c exec -T -e DATABASE_URL="$url" app1 flask --app wsgi:app reconcile
$c exec -T -e DATABASE_URL="$url" app1 flask --app wsgi:app verify-audit
echo "restore verified"
