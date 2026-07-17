"""Production settings for GameKeeperProject.

Selected via DJANGO_SETTINGS_MODULE=GameKeeperProject.settings_prod (set per
service in docker-compose). Adapted for SQLite: the DB is a single file on a
host-mounted /data volume, backed up by copying the file.
"""

import os

from .settings import *  # noqa: F401,F403

DEBUG = False

# Default real deployments to 'production' so the non-production banner (base.html)
# stays off unless a self-hoster deliberately labels this instance via DJANGO_ENV.
ENVIRONMENT = os.environ.get('DJANGO_ENV', 'production')

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# nginx terminates TLS and forwards over plain HTTP; without this Django thinks
# every request is HTTP, breaking the CSRF Origin check (browsers send https).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SECRET_KEY = os.environ['SECRET_KEY']

ALLOWED_HOSTS = os.environ['ALLOWED_HOSTS'].split(',')

# Public origin(s) served by the Cloudflare tunnel, e.g.
# https://games.example.com. Django 4+ checks the Origin header on unsafe
# requests against this list (scheme required, comma-separated in env).
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o]

# SQLite lives on a host-mounted volume (DESIGN §2.1). WAL + busy_timeout are
# applied per-connection in gamekeeper.apps. Backups = copy the .sqlite3 file.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DB_PATH', '/data/db/db.sqlite3'),
        'OPTIONS': {
            'timeout': 5,
        },
    }
}

STATIC_ROOT = '/data/static'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage',
    },
}

MEDIA_ROOT = '/data/media'

BACKUP_DIR = os.environ.get('BACKUP_DIR', '/data/backups')
LOGS_DIR = os.environ.get('LOGS_DIR', '/data/logs')

# Log to console (captured by Docker) plus a rotating file on the logs volume.
LOGGING['handlers']['file'] = {  # noqa: F405
    'class': 'logging.handlers.TimedRotatingFileHandler',
    'filename': os.path.join(LOGS_DIR, 'gamekeeper.log'),
    'when': 'midnight',
    'backupCount': 30,
    'formatter': 'verbose',
    # Defer opening the file until first write so a late /data/logs mount
    # doesn't crash boot.
    'delay': True,
}
LOGGING['loggers']['gamekeeper']['handlers'] = ['console', 'file']  # noqa: F405

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'gamekeeper@example.com')

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/0')

# --- Brute-force login protection (django-axes) ----------------------------
# Lives in prod settings on purpose: every real deployment runs settings_prod,
# so all self-hosters get this for free, while the test suite (base settings)
# keeps using self.client.login() without axes' request requirement.
#
# Locks an account after 5 failed logins for 1 hour. The lockout AUTO-EXPIRES,
# so an honest fat-finger self-heals after the cool-off with no admin action.
# A superuser can also clear it instantly:
#   - Django admin -> Access attempts -> delete the user's row, or
#   - manage.py axes_reset_username <name>   (docker compose exec web ...)
INSTALLED_APPS = INSTALLED_APPS + ['axes']  # noqa: F405
# AxesMiddleware must come last so it sees the final auth outcome.
MIDDLEWARE = MIDDLEWARE + ['axes.middleware.AxesMiddleware']  # noqa: F405

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',  # must precede the model backend
    'django.contrib.auth.backends.ModelBackend',
]

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours — lockout expires on its own so accidents self-heal
# Key lockouts on username, not IP: behind the Cloudflare tunnel + nginx every
# request shares the proxy's IP, so IP-based locking would lock everyone at once.
AXES_LOCKOUT_PARAMETERS = ['username']
AXES_RESET_ON_SUCCESS = True  # a good login clears the running failure count
# Record only lock-worthy failures (no per-login access log) — keeps the
# single-writer SQLite (DESIGN §2.1) essentially untouched by auth traffic.
AXES_DISABLE_ACCESS_LOG = True
# Styled lockout page (extends base.html) that names the cool-off, instead of
# the default bare-text 429. The template reads failure_limit + cooloff_timedelta
# from axes' context, so it stays correct if the limit/cool-off change above.
AXES_LOCKOUT_TEMPLATE = 'registration/lockout.html'
