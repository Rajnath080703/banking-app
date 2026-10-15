#!/bin/sh
# Deploy one environment from its branch. Run on the server (GitHub Actions calls it over SSH):
#   sh /opt/bank/<env>/deploy/server/deploy.sh dev     # branch dev  -> dev.bank.brunocode.in
#   sh /opt/bank/<env>/deploy/server/deploy.sh main    # branch main -> main.bank.brunocode.in
# Takes a database backup first, updates the code, rebuilds, runs migrations, and checks health.
set -eu
# This script updates the checkout it lives in; run from a temporary copy so the update
# cannot change the script while it is executing.
if [ "${BANK_DEPLOY_COPY:-}" != 1 ]; then
  tmp=$(mktemp); cp "$0" "$tmp"
  BANK_DEPLOY_COPY=1 exec sh "$tmp" "$@"
fi
ENV_NAME="$1"
case "$ENV_NAME" in dev|main) ;; *) echo "usage: deploy.sh dev|main" >&2; exit 2 ;; esac
BASE=/opt/bank
DIR="$BASE/$ENV_NAME"
ENV_FILE="$BASE/$ENV_NAME.env"
STACK="docker compose -f $DIR/deploy/server/stack.yml --env-file $ENV_FILE -p bank-$ENV_NAME"
cd "$DIR"

# 1. back up the database before changing anything (skipped on the very first deploy)
mkdir -p "$BASE/backups"
if $STACK ps --status running --services 2>/dev/null | grep -qx db; then
  out="$BASE/backups/bank-${ENV_NAME}_$(date +%Y%m%d_%H%M%S).dump"
  $STACK exec -T db pg_dump -U bankowner -d bankdb --format=custom > "$out"
  echo "backup: $out ($(wc -c < "$out") bytes)"
  ls -1t "$BASE/backups"/bank-"${ENV_NAME}"_*.dump | tail -n +31 | xargs -r rm --   # keep the last 30
fi

# 2. update the code to the branch tip
git fetch --quiet origin "$ENV_NAME"
git checkout --quiet "$ENV_NAME"
git reset --quiet --hard "origin/$ENV_NAME"
echo "code: $(git log -1 --format='%h %s')"

# 3. build, migrate (one-shot service), start
$STACK up -d --build --remove-orphans

# 4. health check through the container (ready = database reachable and schema current)
for i in $(seq 1 30); do
  if $STACK exec -T app python -c "import urllib.request,sys; sys.exit(0 if b'ready' in urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).read() else 1)" 2>/dev/null; then
    echo "bank-$ENV_NAME is ready"; exit 0
  fi
  sleep 3
done
echo "bank-$ENV_NAME did not become ready; recent app logs:" >&2
$STACK logs --tail 50 app >&2
exit 1
