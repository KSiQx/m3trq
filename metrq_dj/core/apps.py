from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        """
        Import signals to register them when Django starts.
        This ensures WAL mode check is active for all database connections.
        """
        import core.signals  # noqa: F401
