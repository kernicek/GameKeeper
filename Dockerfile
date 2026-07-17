FROM python:3.12-slim

WORKDIR /app

# sqlite3 CLI: consistent-snapshot backups (run_backup.sh uses `.backup`).
# gosu: entrypoint chowns /data then drops to PUID:PGID so mounted files are
# owned by the self-hoster's user, not root (DESIGN §2.1 self-host story).
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-prod.txt

COPY . .

# Bake the running version in (issue #95): deploy.yml passes the pushed git tag
# as APP_VERSION, and the app compares it against GHCR's :latest to flag when a
# redeploy is available. Placed after COPY so it never busts the pip layer cache.
ARG APP_VERSION=""
ENV APP_VERSION=${APP_VERSION}

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
