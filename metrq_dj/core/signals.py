"""
Core Django signals for database and infrastructure monitoring.

WAL (Write-Ahead Logging) mode in SQLite significantly improves performance
for concurrent read/write operations, which is critical for web applications.
This signal checks if WAL mode is enabled and logs warnings if it's not.
"""

import logging
from django.db.backends.signals import connection_created
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Track if we've already logged WAL status (per process)
_wal_status_logged = False


@receiver(connection_created)
def check_wal_mode_on_connect(sender, connection, **kwargs):
    """
    Check WAL mode whenever a SQLite database connection is created.
    Logs warning if WAL mode is not enabled (concurrency issues may occur).
    """
    global _wal_status_logged

    if connection.vendor != 'sqlite':
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode;')
            journal_mode = cursor.fetchone()[0]

            if journal_mode.lower() != 'wal':
                logger.warning(
                    f"SQLite connection is not using WAL mode (current: {journal_mode}). "
                    f"Concurrency issues may occur. Enable WAL mode in wsgi.py: "
                    f"cursor.execute('PRAGMA journal_mode=WAL;')"
                )
            elif not _wal_status_logged:
                # Only log success once to avoid spam
                logger.info("SQLite WAL mode is enabled - concurrency optimized.")
                _wal_status_logged = True

    except Exception as e:
        logger.error(f"Could not verify SQLite WAL mode: {e}")
