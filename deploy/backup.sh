#!/bin/sh
# Nightly database dump, run on the server from cron:
#   0 4 * * * /opt/metacritic-game-tracker/deploy/backup.sh >> /var/log/mc-backup.log 2>&1
# Dumps go to ./backups next to the compose files; the last 14 are kept.
set -eu

cd "$(dirname "$0")/.."
mkdir -p backups
file="backups/mc-$(date +%Y-%m-%d_%H%M).sql.gz"

docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
    pg_dump -U mc --clean --if-exists mc | gzip > "$file.tmp"
mv "$file.tmp" "$file"

ls -1t backups/mc-*.sql.gz | tail -n +15 | xargs -r rm --
echo "$(date -Is) backup written: $file ($(du -h "$file" | cut -f1))"
