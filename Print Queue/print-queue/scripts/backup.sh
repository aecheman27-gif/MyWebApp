#!/bin/sh
# Nightly database backup.
# - pg_dump → gzip
# - upload to Cloudflare R2 via the AWS CLI's S3-compatible endpoint
# - keep last 7 daily backups locally as a safety net

set -eu

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR=/tmp/backups
mkdir -p "$BACKUP_DIR"
DUMP_FILE="$BACKUP_DIR/printq-$TIMESTAMP.sql.gz"

echo "[$(date -u +%FT%TZ)] starting backup → $DUMP_FILE"

PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    --host=db \
    --username="$POSTGRES_USER" \
    --dbname="$POSTGRES_DB" \
    --format=plain \
    --no-owner \
    --no-acl \
    | gzip -9 > "$DUMP_FILE"

SIZE=$(du -h "$DUMP_FILE" | awk '{print $1}')
echo "[$(date -u +%FT%TZ)] dump complete, size=$SIZE"

if [ -z "${R2_ACCOUNT_ID:-}" ] || [ -z "${R2_ACCESS_KEY_ID:-}" ] || [ -z "${R2_SECRET_ACCESS_KEY:-}" ]; then
    echo "[$(date -u +%FT%TZ)] R2 credentials not set; skipping upload"
else
    AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    AWS_DEFAULT_REGION=auto \
    aws s3 cp "$DUMP_FILE" "s3://$R2_BUCKET/db/printq-$TIMESTAMP.sql.gz" \
        --endpoint-url "https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com"
    echo "[$(date -u +%FT%TZ)] uploaded to r2://$R2_BUCKET/db/printq-$TIMESTAMP.sql.gz"
fi

# Prune local copies older than 7 days.
find "$BACKUP_DIR" -name 'printq-*.sql.gz' -type f -mtime +7 -delete

echo "[$(date -u +%FT%TZ)] done"
