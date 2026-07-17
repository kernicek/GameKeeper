#!/bin/sh
set -e

# World-readable by default (#101). nginx serves /data/{static,media} as its own
# uid, read-only; Django creates dirs at FILE_UPLOAD_DIRECTORY_PERMISSIONS=None
# (masked by umask), so a tight inherited umask makes cover/static dirs 700/750
# and nginx 403s → no CSS, broken covers. umask 022 → dirs 755, files 644 for
# every write (collectstatic, runtime cover downloads, §7 uploads). Set before
# the gosu re-exec so it carries into all three services.
umask 022

# --- Run as PUID:PGID, not root --------------------------------------------
# The container starts as root so it can fix ownership of the host-mounted
# /data dirs, then drops to PUID:PGID (default 1000:1000) via gosu so the
# SQLite DB, media, logs and backups are owned by the self-hoster's user.
# Set PUID/PGID in the Portainer stack env to match your dataset's owner.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ] && [ -z "$DROPPED_PRIVS" ]; then
  for d in /data/db /data/logs /data/media /data/backups /data/static; do
    [ -d "$d" ] || continue
    # Only touch mismatched entries so restarts stay cheap on a big media tree.
    find "$d" \( ! -uid "$PUID" -o ! -gid "$PGID" \) \
      -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  done
  # DROPPED_PRIVS guards against a re-exec loop if PUID happens to be 0.
  export DROPPED_PRIVS=1
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

case "$SERVICE" in
  web)
    # Only the web service runs migrations, so nothing races the SQLite writer.
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
    # Heal any pre-#101 dirs/files that predate umask 022 or that collectstatic
    # left untouched (unchanged hashed files aren't rewritten). Scoped, not a
    # blanket /data/media recurse: covers only, so we never descend the large
    # /data/media/documents HDD mount (BULK_ROOT, different ownership).
    chmod -R a+rX /data/static
    [ -d /data/media/covers ] && chmod -R a+rX /data/media/covers 2>/dev/null || true
    exec gunicorn GameKeeperProject.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers 2 \
      --timeout 120
    ;;
  celery-worker)
    # concurrency=1 (DESIGN §2.1): the only background writer is BGG sync, which
    # is hard rate-limited and effectively serial, so a single worker sidesteps
    # SQLite write-lock contention.
    exec celery -A GameKeeperProject worker \
      --loglevel=info \
      --concurrency=1
    ;;
  celery-beat)
    exec celery -A GameKeeperProject beat \
      --loglevel=info \
      --schedule=/tmp/celerybeat-schedule
    ;;
  *)
    echo "Unknown SERVICE: $SERVICE"
    echo "Set SERVICE to: web, celery-worker, or celery-beat"
    exit 1
    ;;
esac
