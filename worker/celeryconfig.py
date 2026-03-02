"""
Celery Configuration for MetrQ Worker
Supports tier-based queue routing and scheduled tasks.
"""

import os
from celery import Celery
from celery.signals import task_failure, task_success
from kombu import Queue, Exchange
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

app = Celery('metrq')
app.config_from_object('django.conf:settings', namespace='CELERY')

# Queue Configuration
app.conf.task_queues = (
    Queue('default', Exchange('default'), routing_key='default'),
    Queue('reports_fast', Exchange('reports'), routing_key='reports.fast'),
    Queue('reports_slow', Exchange('reports'), routing_key='reports.slow'),
    Queue('batch', Exchange('batch'), routing_key='batch'),
    Queue('provider', Exchange('provider'), routing_key='provider'),
    Queue('db_writer', Exchange('db_writer'), routing_key='db_writer'),
    Queue('llm_tasks', Exchange('llm'), routing_key='llm.tasks'),
    Queue('llm_extraction', Exchange('llm'), routing_key='llm.extraction'),
)

app.conf.task_default_queue = 'default'
app.conf.task_default_exchange = 'default'
app.conf.task_default_routing_key = 'default'

# Task Routing
app.conf.task_routes = {
    'worker.tasks.reports.generate_pdf_report': {'queue': 'reports_fast', 'routing_key': 'reports.fast'},
    'worker.tasks.reports.generate_pdf_report_free': {'queue': 'reports_slow', 'routing_key': 'reports.slow'},
    'worker.tasks.reports.generate_excel_report': {'queue': 'reports_fast', 'routing_key': 'reports.fast'},
    'worker.tasks.reports.generate_excel_report_free': {'queue': 'reports_slow', 'routing_key': 'reports.slow'},
    'worker.tasks.batch.*': {'queue': 'batch', 'routing_key': 'batch'},
    'worker.tasks.provider.*': {'queue': 'provider', 'routing_key': 'provider'},
    'modules.analytics.storage.run_db_writer_batch': {'queue': 'db_writer', 'routing_key': 'db_writer'},
    'worker.tasks.importance.*': {'queue': 'llm_tasks', 'routing_key': 'llm.tasks'},
    'core.tasks.extraction.*': {'queue': 'llm_extraction', 'routing_key': 'llm.extraction'},
}

# Configuration
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
    result_backend=os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0'),
    result_expires=3600,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    task_default_retry_delay=60,
    task_max_retries=3,
)

# Scheduled Tasks
app.conf.beat_schedule = {
    # Existing tasks
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
    # IMPORTANT: For production with single GPU, workers processing 'llm_tasks'
    # should run with CELERY_WORKER_CONCURRENCY=1 to prevent GPU overload.
    # Example: celery -A worker worker -Q llm_tasks --concurrency=1
    'classify_importance_batch': {
        'task': 'worker.tasks.importance.classify_importance_batch',
        'schedule': getattr(settings, 'IMPORTANCE_POLL_INTERVAL', 300.0),  # 5 minutes default
        'options': {'queue': 'llm_tasks'}
    },
    'extract_articles': {
        'task': 'core.tasks.extraction.batch_extract_articles',
        'schedule': 300.0,  # Every 5 minutes
        'kwargs': {'limit': 10}
    },
    'cleanup_stale_locks': {
        'task': 'modules.analytics.storage.cleanup_stale_job_locks',
        'schedule': 300.0,  # Every 5 minutes
    },
    'cleanup_stale_jobs': {
        'task': 'worker.tasks.provider.cleanup_stale_jobs',
        'schedule': 300.0,
        'options': {'queue': 'provider'}
    },
    'cleanse_old_logs': {
        'task': 'worker.tasks.provider.cleanse_old_logs',
        'schedule': 'crontab(hour=3, minute=0)',
        'options': {'queue': 'provider'}
    },
    'rotate_expired_keys': {
        'task': 'worker.tasks.provider.rotate_expired_keys',
        'schedule': 'crontab(hour=4, minute=0)',
        'options': {'queue': 'provider'}
    },
    'system_health_check': {
        'task': 'worker.tasks.provider.system_health_check',
        'schedule': 300.0,
        'options': {'queue': 'provider'}
    },
    'cleanup_old_reports': {
        'task': 'worker.tasks.reports.cleanup_old_reports',
        'schedule': 'crontab(hour=2, minute=0)',
        'options': {'queue': 'default'}
    },
}

app.autodiscover_tasks(['worker.tasks.batch', 'worker.tasks.provider', 'worker.tasks.reports', 'modules.analytics.storage'])

@task_failure.connect
def handle_task_failure(sender, task_id, exception, args, kwargs, traceback, einfo, **extras):
    import structlog
    logger = structlog.get_logger()
    logger.error("task_failed", task_name=sender.name if sender else 'unknown', task_id=task_id, exception=str(exception))

@task_success.connect
def handle_task_success(sender, result, **kwargs):
    import structlog
    logger = structlog.get_logger()
    logger.info("task_succeeded", task_name=sender.name if sender else 'unknown')


# from celery import Celery
# import os
#
# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')
#
# app = Celery('metrq')
# app.config_from_object('django.conf:settings', namespace='CELERY')
# app.autodiscover_tasks()
#
# app.conf.beat_schedule = {
#     'fetch_rss': {
#         'task': 'worker.tasks.batch.fetch_and_enqueue',
#         'schedule': 60.0 * 5,  # Every 5 minutes
#     },
#     'cleanup_stale': {
#         'task': 'worker.tasks.batch.cleanup_stale_jobs',
#         'schedule': 60.0 * 5,  # Every 5 minutes
#     },
#     'db_writer_batch': {
#         'task': 'modules.analytics.storage.run_db_writer_batch',
#         'schedule': 10.0,  # Every 10 seconds
#         'options': {'queue': 'db_writer'}
#     },
#     'cleanup_stale_locks': {
#         'task': 'modules.analytics.storage.cleanup_stale_job_locks',
#         'schedule': 300.0,  # Every 5 minutes
#     },
# }
