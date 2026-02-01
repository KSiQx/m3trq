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
}
