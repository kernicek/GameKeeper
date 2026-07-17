# GameKeeper

*This project is in very early stages of development*

A self-hosted board-game tracker: collection, editions/copies, purchases and
crowdfunding pledge managers, sleeve/card-size math, and BGG sync. Django +
SQLite + Celery, packaged as a single container image and deployed with
docker-compose.

- **Local development:** `python manage.py runserver` against the base settings
  (`GameKeeperProject.settings`, SQLite in the repo).
- **Production:** `GameKeeperProject.settings_prod`, run from the published
  image via [docker-compose.yml](docker-compose.yml). See below.

## Deployment (TrueNAS SCALE + Portainer)

The NAS never builds the image. [GitHub Actions](.github/workflows/deploy.yml)
builds and pushes `ghcr.io/kernicek/gamekeeper` on a **version tag**, and
Portainer pulls it. TLS is terminated by a Cloudflare Tunnel at the edge; nginx
serves plain HTTP inside. Storage is split: an SSD dataset (`DATA_ROOT`) holds
the DB, cover media, static and logs; an HDD dataset (`BULK_ROOT`) holds
backups and bulky document uploads (rulebooks, PnP files), which are
nested-mounted into the media tree at `/data/media/documents` so their public
URLs stay under `/media/`.

### 0. Build the image (once per release)

```sh
git tag 2026.07.05 && git push origin 2026.07.05   # CalVer; CI builds on tags only
```

CI produces `ghcr.io/kernicek/gamekeeper:<tag>` and `:latest`. The stack
defaults to `:latest`; pin `IMAGE_TAG` to a specific tag when you want
deterministic rollbacks.

### 1. Create the datasets

Create two datasets in the TrueNAS UI, then the subdirs the compose volumes
expect. Substitute your own PUID/PGID (the user that should own the data):

```sh
# SSD dataset — DB, logs, static, cover media
mkdir -p <path-to-data>/gamekeeper/{db,logs,static,media}
# HDD dataset — backups + document uploads (nested at /data/media/documents)
mkdir -p <path-to-bulk-data>/gamekeeper/{backups,documents}
chown -R 1000:1000 <path-to-data>/gamekeeper <path-to-bulk-data>/gamekeeper
```

**Document uploads need an `everyone@` read ACL on the HDD dataset.** nginx serves
`/media/documents/` as its own uid (not `PUID`, not in `PGID`), so it relies on a
world/`everyone@` read+traverse bit. The app image sets `umask 022` so everything it
writes is world-readable, but TrueNAS SCALE datasets default to `acltype=nfsv4` +
`aclmode=restricted`, which makes ZFS **veto POSIX `chmod`** — a container-side
`chmod a+rX` on the documents tree fails with "Operation not permitted" even as root,
and nginx then **403**s every document (no CSS/cover symptom — those live on the SSD
dataset). Fix it on the `documents` (`BULK_ROOT`) dataset in the TrueNAS UI: **Datasets →
Edit ACL** → add an ACE `everyone@` with **Read + Execute (traverse)**, inherited by
children (apply recursively). Equivalent CLI:

```sh
setfacl -R -m everyone@:rxaRc:fd:allow <path-to-bulk-data>/gamekeeper/documents
```

The SSD dataset (static + covers) usually allows `chmod`, so the image's built-in
`chmod -R a+rX` heal covers it; only the HDD `documents` dataset needs this ACL step.

nginx's config must live on a host path the Docker daemon can see (a Git-stack
checkout lives inside Portainer and isn't visible to the daemon), so copy
`nginx/nginx.conf` from the repo onto the SSD dataset — as a **file**, or Docker
will create a directory there and fail to mount it:

```sh
# e.g. scp from your local checkout to the NAS
scp nginx/nginx.conf <nas>:<path-to-data>/gamekeeper/nginx.conf
```

### 2. Prepare the environment variables

Everything the stack needs — deploy knobs *and* app secrets — is supplied as
Portainer **stack Environment variables**. There is no `app.env` file on disk: a
host `env_file` isn't visible to Portainer's compose parser (it runs inside the
Portainer container), so the values are passed in through Portainer instead.

Generate a secret key:

```sh
python -c "import secrets; print(secrets.token_urlsafe(64))"   # -> SECRET_KEY
```

You'll paste the block below into the stack in step 5. Fill in the blanks first
(adjust the paths/port to your datasets):

```ini
# --- deploy knobs ---
IMAGE_TAG=latest
WEB_PORT=8282  # change to whatever host port you want to expose
PUID=1000
PGID=1000

# --- required app config (deploy fails fast if any is blank) ---
DATA_ROOT=<path-to-data>/gamekeeper
BULK_ROOT=<path-to-bulk-data>/gamekeeper
SECRET_KEY=
ALLOWED_HOSTS=gamekeeper.yourdomain.com
CSRF_TRUSTED_ORIGINS=https://gamekeeper.yourdomain.com

# --- email (optional; password-reset + reminder mail) ---
EMAIL_HOST=smtp.yourprovider.com
EMAIL_PORT=587
EMAIL_HOST_USER=you@example.com
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=you+gamekeeper@example.com

# --- BGG sync (optional; per-user creds override these) ---
BGG_API_TOKEN=
BGG_USERNAME=
BGG_PASSWORD=
BGG_ENCRYPTION_KEY=

# --- ntfy push notifications (optional) ---
NTFY_SERVER_URL=
NTFY_AUTH_TOKEN=
```

`ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` are your tunnel domain (step 6).
`DATA_ROOT` and `BULK_ROOT` have no default either — all five required vars
stop the deploy rather than silently falling back to some other path or
booting insecure.

The `BGG_*` account vars are now a **fallback/bootstrap** only: each user sets
their own BGG username + password in-app under **BGG account** (nav) — the
password is stored encrypted at rest. `BGG_ENCRYPTION_KEY` is the Fernet key for
that encryption; leave it blank to derive one from `SECRET_KEY` (fine for most),
or set a stable dedicated key (`python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())"`) so stored passwords survive a
`SECRET_KEY` rotation. Keep it out of your DB backups.

`NTFY_SERVER_URL` is the base URL of your self-hosted **ntfy** server, including
port (e.g. `http://<your-ntfy-host>:<port>`) — leave it blank to keep push
notifications off. The topic each user pushes to is set per-user in-app under
**Settings**, not here, so reminders don't cross between users on a shared
deployment. If your ntfy server requires auth, create a dedicated user scoped
to write-only access on the topic(s), generate an access token for it (`ntfy
token add <user>`), and set `NTFY_AUTH_TOKEN` — sent as a Bearer token. Leave
it blank if your ntfy instance allows anonymous publish.

The superuser "newer image available" navbar icon compares the running image
against `ghcr.io/kernicek/gamekeeper:latest` anonymously, since the package is
public. The icon only ever shows to superusers, and silently stays hidden if
the check can't run.

### 3. Seed the database

Copy your existing SQLite DB into place (migrations run automatically on boot):

```sh
cp db.sqlite3 <path-to-data>/gamekeeper/db/db.sqlite3
chown 1000:1000 <path-to-data>/gamekeeper/db/db.sqlite3
```

Fresh install instead? Skip this — the first boot migrates an empty DB. Then
create an admin user once the stack is up:

```sh
docker compose exec web python manage.py createsuperuser
```

### 4. Give Portainer registry access (private image)

The GHCR package is private, so Portainer must authenticate to pull it.

1. Create a GitHub PAT (Personal Access Token) with **`read:packages`**.
2. Portainer → **Registries → Add registry → Custom**: URL `ghcr.io`, username =
   your GitHub username, password = the PAT.

The repo is also private, so if you use a git-based stack (step 5) add a PAT with
**`repo`/`contents:read`** under the stack's Git authentication too.

### 5. Create the stack

Portainer → **Stacks → Add stack → Git repository**:

- Repository URL `https://github.com/kernicek/GameKeeper.git`, ref `master`,
  compose path `docker-compose.yml`. Enable Git **Authentication** — the repo is
  private (step 4).
- **Environment variables → Advanced mode:** paste the filled-in block from
  step 2.

Deploy the stack. The entrypoint chowns `/data`, drops to `PUID:PGID`, then the
`web` service runs `migrate` + `collectstatic` and starts gunicorn.

### 6. Cloudflare Tunnel

Run a `cloudflared` tunnel (separate container or host service) pointing your
public hostname at `http://<nas-ip>:<WEB_PORT>`. nginx already forwards
`X-Forwarded-Proto: https` so Django's secure cookies and CSRF Origin check work.
Then set both `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (scheme-qualified) to
your tunnel domain in the stack env vars, and redeploy.

> Secure cookies mean **authenticated access requires the HTTPS tunnel domain** —
> hitting `http://<nas-ip>:<WEB_PORT>` directly on the LAN is browse-only; logins
> won't stick. Enable HSTS at the Cloudflare edge rather than in Django.

### 7. Schedule backups

Add TrueNAS cron jobs (System Settings → Advanced → Cron Jobs), running as the
`PUID` user from the stack's checkout. Pass the same `DATA_ROOT`/`BULK_ROOT`:

```sh
# daily full
0 3 * * *   DATA_ROOT=<path-to-data>/gamekeeper BULK_ROOT=<path-to-bulk-data>/gamekeeper sh /path/to/repo/run_backup.sh full
# hourly selective
0 * * * *   DATA_ROOT=<path-to-data>/gamekeeper BULK_ROOT=<path-to-bulk-data>/gamekeeper sh /path/to/repo/run_backup.sh selective
```

See [run_backup.sh](run_backup.sh) for the WAL-safe SQLite snapshot + media
archive details. Failures are always logged to `${DATA_ROOT}/logs/backup.log`;
*email* alerts on failure are optional and need SMTP creds — since app config now
lives in Portainer (not on disk), drop a small `${DATA_ROOT}/app.env` with just
`EMAIL_*` + `BACKUP_PUSH_NOTIFY_EMAIL` (see [.env.example](.env.example)) if you
want them.

### Updating & rollback

Ship a new release by pushing a new tag. If you run `IMAGE_TAG=latest` (the
default), just **re-pull** the stack in Portainer to pick it up. To pin or roll
back, set `IMAGE_TAG` to a specific tag and re-pull — old images stay in GHCR.

### Locked out?

django-axes locks an account after 5 failed logins for 1 hour (auto-expires). To
clear it immediately: `docker compose exec web python manage.py axes_reset_username <name>`.
