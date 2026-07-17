"""
Django settings for GameKeeperProject.

Base (development) settings. Production overrides live in settings_prod.py,
selected via DJANGO_SETTINGS_MODULE=GameKeeperProject.settings_prod.

Infrastructure conventions adapted for SQLite (WAL + busy_timeout,
single-concurrency sync worker) per DESIGN §2.1.
"""

import os
import sys
from pathlib import Path

from celery.schedules import crontab

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
except ImportError:
    pass


# SECURITY WARNING: keep the secret key used in production secret! Production
# (settings_prod.py) requires SECRET_KEY from the environment and fails to boot
# without it; this fallback only ever runs under DEBUG=True local dev.
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-me-for-local-dev-only')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ENVIRONMENT = os.environ.get('DJANGO_ENV', 'development')

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'gamekeeper.apps.GamekeeperConfig',
    'simple_history',
    'django_celery_results',
    'impersonate',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # After auth (needs request.user) and before HistoryRequestMiddleware so
    # simple-history attributes edits made while impersonating to the
    # impersonated user — correct, since impersonation allows actions (#108).
    'impersonate.middleware.ImpersonateMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware',
]

ROOT_URLCONF = 'GameKeeperProject.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'gamekeeper.context_processors.environment',
                'gamekeeper.context_processors.impersonation',
                'gamekeeper.context_processors.update_notice',
            ],
        },
    },
]

# Superuser impersonation (issue #108, via django-impersonate). Locked down at
# the library level: only superusers may start impersonation, and no superuser
# can be impersonated — so impersonation can never grant more than the real
# superuser already has (no privilege escalation). The library default is the
# opposite (REQUIRE_SUPERUSER=False), so this dict is load-bearing security.
IMPERSONATE = {
    'REQUIRE_SUPERUSER': True,
    'ALLOW_SUPERUSER': False,
}

WSGI_APPLICATION = 'GameKeeperProject.wsgi.application'


# Database — SQLite (DESIGN §2.1). WAL + busy_timeout are applied on every new
# connection by gamekeeper.apps (connection_created signal). `timeout` here is
# the SQLite busy_timeout in seconds, applied at connect time as a belt-and-braces.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DB_PATH', BASE_DIR / 'db.sqlite3'),
        'OPTIONS': {
            'timeout': 5,
        },
    }
}


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Under test, the default PBKDF2 hasher costs ~0.6 s per hash, and the suite's
# create_user()/client.login() calls (logins run per test method) add up to
# minutes of pure hashing. MD5 here applies to `manage.py test` only —
# production hashing is untouched.
if 'test' in sys.argv:
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


# Internationalization

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Europe/Prague'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Media (local filesystem volume; DESIGN §2/§7)
MEDIA_URL = '/media/'
MEDIA_ROOT = str(BASE_DIR / 'media')

# #113: make every media-directory write world-traversable (755) regardless of the
# writer's umask, so a cover command run from a root shell (Portainer console) can't
# bake a 700 covers/previews/ dir that nginx (a different uid) 403s. Files already
# default to 644 in Django; pin both explicitly to document the a+rX intent that the
# entrypoint's `chmod -R a+rX` only heals on the next web restart.
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755

# §7 documents: per-file upload cap and the allowed-extension allowlist.
# External-link-only documents cost no storage and bypass both checks.
DOCUMENT_MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB
DOCUMENT_ALLOWED_EXTENSIONS = [
    'pdf', 'zip', 'png', 'jpg', 'jpeg', 'gif',
    'txt', 'md', 'docx', 'xlsx', 'stl', 'epub',
]

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# Sliding session timeout: resets on activity; idle sessions expire after a week.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 7 days
SESSION_SAVE_EVERY_REQUEST = True


# --- BGG integration (DESIGN §8) --------------------------------------------
# One admin-configured instance-wide credential; per-user BGG username lives on
# the user later. Read-only. When set, BGG_API_TOKEN is sent as an
# Authorization: Bearer header (§8 phase 2) and unlocks the token-gated /thing
# (weight + out-of-collection stats).
BGG_API_TOKEN = os.environ.get('BGG_API_TOKEN', '')

# Token-less fallback (§8): with no token, sync_bgg logs in with this BGG
# account — own-collection downloads are exempt from app registration. Still
# usable for self-hosters who don't register an app. BGG_USERNAME also names
# whose collection to pull, so it is required in both modes. Secrets live in
# .env only; they are never logged.
BGG_USERNAME = os.environ.get('BGG_USERNAME', '')
BGG_PASSWORD = os.environ.get('BGG_PASSWORD', '')

# Per-user BGG passwords (issue #118) are stored Fernet-encrypted at rest. The
# key comes from BGG_ENCRYPTION_KEY when set (a urlsafe-base64 32-byte Fernet
# key); otherwise it is derived deterministically from SECRET_KEY. Either way it
# lives only in the environment, never in the DB backup, and survives restarts.
# Rotating SECRET_KEY without a stable BGG_ENCRYPTION_KEY invalidates stored
# passwords (users re-enter them). See gamekeeper/crypto.py.
BGG_ENCRYPTION_KEY = os.environ.get('BGG_ENCRYPTION_KEY', '')


# --- ntfy push notifications (issue #162) -----------------------------------
# Base URL of the self-hosted ntfy server (e.g. "http://192.168.1.42:8234").
# Blank by default: dev/test stay fully offline, and self-hosters who don't
# run ntfy get no network calls at all. Per-user topic lives on Membership.
NTFY_SERVER_URL = os.environ.get('NTFY_SERVER_URL', '')

# Access token for a locked-down ntfy server (Settings > Users > `ntfy token
# add`). Sent as a Bearer token; blank means no Authorization header, for
# ntfy instances left open to anonymous publish.
NTFY_AUTH_TOKEN = os.environ.get('NTFY_AUTH_TOKEN', '')


# --- Update notice (issue #95) ----------------------------------------------
# The running image's version, baked in at build time (Dockerfile ARG ->
# ENV APP_VERSION, fed from the pushed git tag in deploy.yml). Empty in dev, so
# the update check stays dark locally and never touches the network.
APP_VERSION = os.environ.get('APP_VERSION', '')
# The GHCR image to compare against. Self-hosters who build their own image
# override this to point the check at their own registry/repo.
GHCR_IMAGE = os.environ.get('GHCR_IMAGE', 'ghcr.io/kernicek/gamekeeper')
# Optional read:packages token. Needed only while the GHCR package is private
# (issue #100); once it is public the anonymous ghcr.io token-exchange suffices
# and this can stay blank. Secret lives in the environment only, never logged.
GHCR_TOKEN = os.environ.get('GHCR_TOKEN', '')


# --- Celery (DESIGN §2) -----------------------------------------------------
CELERY_TIMEZONE = 'Europe/Prague'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_RESULT_BACKEND = 'django-db'
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
# BGG sync (DESIGN §8) will add its schedule here too.
CELERY_BEAT_SCHEDULE = {
    # DESIGN §11 email reminders: pledge managers closing soon + watched
    # campaigns ending soon. Daily; ReminderLog makes re-fires no-ops.
    'send-reminder-emails': {
        'task': 'gamekeeper.tasks.send_reminder_emails',
        'schedule': crontab(hour=8, minute=0),
    },
}


# --- Email (DESIGN §2/§11): console in dev, SMTP in prod --------------------
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'gamekeeper': {
            'handlers': ['console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
    },
}
