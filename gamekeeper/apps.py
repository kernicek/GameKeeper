from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs):
    """Apply SQLite pragmas on every new connection (DESIGN §2.1).

    WAL lets concurrent readers proceed without blocking the single writer;
    busy_timeout makes writers wait for a lock instead of erroring; NORMAL
    synchronous is the safe/fast pairing with WAL.
    """
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=5000;')
        cursor.execute('PRAGMA foreign_keys=ON;')


class GamekeeperConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'gamekeeper'

    def ready(self):
        connection_created.connect(_configure_sqlite, dispatch_uid='gamekeeper_sqlite_pragmas')
        from . import signals  # noqa: F401 — connects the §3 auto-group receiver
