#!/usr/bin/env bash
# Daily dump of the Citadel database (run by the citadel-backup systemd timer, 03:30 UTC).
# Keeps 7 days under /var/backups/citadel and, if BACKUP_S3_BUCKET is set, copies each to S3.
# Restore:  gunzip -c citadel-YYYYMMDDTHHMMSSZ.sql.gz | docker compose -p citadel ... exec -T postgres psql -U citadel -d citadel
set -euo pipefail
APP_DIR=/opt/citadel
DEST=/var/backups/citadel
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"
docker compose -p citadel --env-file "$APP_DIR/.env.aws" -f "$APP_DIR/deploy/aws/docker-compose.yml" \
  exec -T postgres pg_dump -U citadel -d citadel | gzip > "$DEST/citadel-$STAMP.sql.gz"
find "$DEST" -name 'citadel-*.sql.gz' -mtime +7 -delete
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  aws s3 cp "$DEST/citadel-$STAMP.sql.gz" "s3://$BACKUP_S3_BUCKET/citadel/"
fi
echo "backup written: $DEST/citadel-$STAMP.sql.gz ($(du -h "$DEST/citadel-$STAMP.sql.gz" | cut -f1))"
