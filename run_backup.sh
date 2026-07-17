#!/bin/sh
# Snapshot the SQLite DB (DESIGN §2.1: "backups are copy the .sqlite3 file") and
# the media tree into the HDD backups dataset. TrueNAS is the host here, so there
# is no separate NAS to push to: the compose stack already mounts ${DATA_ROOT}
# (SSD: db + media + app.env) and ${BULK_ROOT}/backups (HDD) into the web
# container, and a consistent snapshot is taken with `sqlite3 .backup` (WAL-safe)
# from inside that container straight onto the backups volume.
#
# Schedule from the TrueNAS UI (System Settings -> Advanced -> Cron Jobs), running
# as the DATA_ROOT owner (PUID) from this repo checkout. Pass the same DATA_ROOT /
# BULK_ROOT you set in the Portainer stack so the paths line up:
#   full (daily):       0 3 * * *   DATA_ROOT=/mnt/ssd/apps/gamekeeper BULK_ROOT=/mnt/tank/apps/gamekeeper sh /path/run_backup.sh full
#   selective (hourly): 0 * * * *   DATA_ROOT=/mnt/ssd/apps/gamekeeper BULK_ROOT=/mnt/tank/apps/gamekeeper sh /path/run_backup.sh selective
#
# `docker compose exec` targets the running stack. If this checkout's directory
# name differs from the Portainer stack/project name, point DC at the real one:
#   DC="docker compose -p <stack-name>"   (or add -f /path/to/compose.yml)
set -u

MODE="${1:-}"
case "$MODE" in
  full|selective) ;;
  *)
    echo "Usage: sh run_backup.sh full|selective" >&2
    exit 2
    ;;
esac

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR" || exit 1

# Host dataset roots — default to docker-compose.yml's defaults; override via env
# (or the cron command) to match your Portainer stack's DATA_ROOT/BULK_ROOT.
DATA_ROOT="${DATA_ROOT:-/mnt/ssd/apps/gamekeeper}"
BULK_ROOT="${BULK_ROOT:-/mnt/tank/apps/gamekeeper}"
DC="${DC:-docker compose}"

BACKUP_DIR="$BULK_ROOT/backups"
LOG_FILE="$DATA_ROOT/logs/backup.log"
# SMTP creds for failure alerts live in app.env on the SSD dataset (the same
# env_file the containers load), not in a repo-local .env.
ENV_FILE="$DATA_ROOT/app.env"

# The web container writes snapshots to its /data/backups mount, which is
# $BACKUP_DIR on the host.
DB_SNAPSHOT="$BACKUP_DIR/gamekeeper_db_latest.sqlite3"
MEDIA_ARCHIVE="$BACKUP_DIR/gamekeeper_media_latest.tar.gz"

# Retention: dailies share one dir; hourlies bucket by day so a bad day is
# recoverable without one directory growing unbounded.
if [ "$MODE" = full ]; then
  DEST_DIR="$BACKUP_DIR/db_daily"
else
  DEST_DIR="$BACKUP_DIR/db_hourly/$(date '+%Y-%m-%d')"
fi
STAMP=$(date '+%Y-%m-%d_%H%M%S')

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
OVERALL_STATUS=success
FAILURE_DETAIL=""

send_failure_email() {
  if [ ! -f "$ENV_FILE" ]; then
    echo "$TIMESTAMP run_backup($MODE): cannot send failure email, $ENV_FILE not found" >> "$LOG_FILE"
    return 1
  fi
  smtp_host=$(grep '^EMAIL_HOST=' "$ENV_FILE" | cut -d= -f2-)
  smtp_port=$(grep '^EMAIL_PORT=' "$ENV_FILE" | cut -d= -f2-)
  smtp_user=$(grep '^EMAIL_HOST_USER=' "$ENV_FILE" | cut -d= -f2-)
  smtp_pass=$(grep '^EMAIL_HOST_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
  from_addr=$(grep '^DEFAULT_FROM_EMAIL=' "$ENV_FILE" | cut -d= -f2-)
  to_addr=$(grep '^BACKUP_PUSH_NOTIFY_EMAIL=' "$ENV_FILE" | cut -d= -f2-)
  if [ -z "$to_addr" ]; then
    echo "$TIMESTAMP run_backup($MODE): BACKUP_PUSH_NOTIFY_EMAIL not set, skipping failure email" >> "$LOG_FILE"
    return 1
  fi
  mail_body=$(printf 'From: %s\r\nTo: %s\r\nSubject: GameKeeper backup failed (%s)\r\n\r\nrun_backup.sh %s failed.\r\n\r\nDetail: %s\r\nTime: %s\r\n' \
    "$from_addr" "$to_addr" "$MODE" "$MODE" "$FAILURE_DETAIL" "$TIMESTAMP")
  printf '%s' "$mail_body" | curl -s --ssl-reqd \
    --url "smtp://$smtp_host:$smtp_port" \
    --mail-from "$from_addr" --mail-rcpt "$to_addr" \
    --user "$smtp_user:$smtp_pass" --upload-file -
}

# 1. WAL-safe SQLite snapshot, written straight to the backups dataset.
$DC exec -T web sh -c \
  "sqlite3 /data/db/db.sqlite3 \".backup '/data/backups/gamekeeper_db_latest.sqlite3'\"" \
  >> "$LOG_FILE" 2>&1
snap_rc=$?
if [ "$snap_rc" -ne 0 ]; then
  OVERALL_STATUS=failure
  FAILURE_DETAIL="sqlite .backup failed (rc=$snap_rc)"
fi

# 2. Archive the media tree from inside the container (it is mounted at
#    /data/media), so this does not depend on the cron user's host perms.
if [ "$OVERALL_STATUS" = success ]; then
  $DC exec -T web sh -c \
    "tar -czf /data/backups/gamekeeper_media_latest.tar.gz -C /data media" \
    >> "$LOG_FILE" 2>&1
  tar_rc=$?
  if [ "$tar_rc" -ne 0 ]; then
    OVERALL_STATUS=failure
    FAILURE_DETAIL="tar of media failed (rc=$tar_rc)"
  fi
fi

# 3. File the two snapshots into the retention bucket (already on the HDD).
if [ "$OVERALL_STATUS" = success ]; then
  if ! mkdir -p "$DEST_DIR" \
     || ! cp -f "$DB_SNAPSHOT" "$DEST_DIR/gamekeeper_db_$STAMP.sqlite3" \
     || ! cp -f "$MEDIA_ARCHIVE" "$DEST_DIR/gamekeeper_media_$STAMP.tar.gz"; then
    OVERALL_STATUS=failure
    FAILURE_DETAIL="filing snapshots into $DEST_DIR failed"
  fi
fi

if [ "$OVERALL_STATUS" = success ]; then
  echo "$TIMESTAMP run_backup($MODE): success - filed into $DEST_DIR" >> "$LOG_FILE"
  exit 0
else
  echo "$TIMESTAMP run_backup($MODE): FAILURE - $FAILURE_DETAIL" >> "$LOG_FILE"
  send_failure_email
  exit 1
fi
