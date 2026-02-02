from celery import Celery
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

app = Celery('metrq')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'fetch_rss': {
        'task': 'worker.tasks.batch.fetch_and_enqueue',
        'schedule': 60.0 * 5,  # Every 5 minutes
    },
    'cleanup_stale': {
        'task': 'worker.tasks.batch.cleanup_stale_jobs',
        'schedule': 60.0 * 5,  # Every 5 minutes
    },
    'db_writer_batch': {
        'task': 'modules.analytics.storage.run_db_writer_batch',
        'schedule': 10.0,  # Every 10 seconds
        'options': {'queue': 'db_writer'}
    },
    'cleanup_stale_locks': {
        'task': 'modules.analytics.storage.cleanup_stale_job_locks',
        'schedule': 300.0,  # Every 5 minutes
    },
}
