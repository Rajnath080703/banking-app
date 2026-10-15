#!/bin/sh
# Consistent online backup (custom format, compressed) of the production database.
#   deploy/backup.sh            -> backups/bankdb_YYYYmmdd_HHMMSS.dump
# Schedule daily; copy the file off the host (encrypted object storage) and keep 35 days.
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/bankdb_$(date +%Y%m%d_%H%M%S).dump"
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod exec -T db \
  sh -c 'pg_dump -U bankowner -d bankdb --format=custom --compress=9' > "$out"
echo "backup written: $out ($(wc -c < "$out") bytes)"
