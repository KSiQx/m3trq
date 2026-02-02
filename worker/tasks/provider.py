"""
Celery tasks for Provider Control Panel operations.
Scheduled maintenance, cleanup, and monitoring tasks.
"""
import os
import sys
import asyncio
from datetime import timedelta
from pathlib import Path

# Django setup
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'metrq_dj'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

import django

django.setup()

from celery import shared_task
from django.utils import timezone
from django.db import transaction, connection
from django.db.models import F

from core.models import Job, ProviderLog, ProviderApiKey

import structlog

logger = structlog.get_logger(__name__)


# ============================================================================
# STALE JOB CLEANUP
# ============================================================================

@shared_task(bind=True, max_retries=3)
def cleanup_stale_jobs(self):
    """
    Scheduled task: Reset jobs stuck in processing state (>5 minutes).
    Indicates crashed workers. Run every 10 minutes via Celery Beat.
    """
    stale_cutoff = timezone.now() - timedelta(minutes=5)
    reset_ids = []

    try:
        # Atomic update for SQLite
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('BEGIN IMMEDIATE')
                cursor.execute("""
                    UPDATE core_job 
                    SET status = 'pending', 
                        locked_by = NULL, 
                        locked_at = NULL,
                        retries = retries + 1
                    WHERE status = 'processing' 
                      AND locked_at < %s
                    RETURNING id, locked_by
                """, [stale_cutoff])

                rows = cursor.fetchall()
                cursor.execute('COMMIT')

                for row in rows:
                    job_id, worker_id = row
                    reset_ids.append(str(job_id))
                    logger.warning("stale_job_reset",
                                   job_id=str(job_id),
                                   worker_id=worker_id,
                                   stale_minutes=5)
        else:
            # PostgreSQL path
            with transaction.atomic():
                stale_jobs = Job.objects.select_for_update().filter(
                    status='processing',
                    locked_at__lt=stale_cutoff
                )

                for job in stale_jobs:
                    reset_ids.append(str(job.id))
                    logger.warning("stale_job_reset",
                                   job_id=str(job.id),
                                   worker_id=job.locked_by)

                stale_jobs.update(
                    status='pending',
                    locked_by=None,
                    locked_at=None,
                    retries=F('retries') + 1
                )

        # Log summary if any reset
        if reset_ids:
            ProviderLog.objects.create(
                level='warning',
                message=f'Auto-cleanup reset {len(reset_ids)} stale jobs',
                data={
                    'job_ids': reset_ids,
                    'threshold_minutes': 5
                },
                worker_id='celery_cleanup'
            )

        return f"Reset {len(reset_ids)} stale jobs"

    except Exception as e:
        logger.error("cleanup_failed", error=str(e))
        # Retry with backoff
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# LOG CLEANSE
# ============================================================================

@shared_task
def cleanse_old_logs():
    """
    Daily task: Delete provider logs older than 7 days.
    Prevents infinite DB growth. Spec requirement: 7-day TTL.
    """
    cutoff = timezone.now() - timedelta(days=7)

    try:
        deleted, _ = ProviderLog.objects.filter(timestamp__lt=cutoff).delete()

        if deleted > 0:
            logger.info("old_logs_cleansed", count=deleted, older_than_days=7)

            # Log the cleanse action
            ProviderLog.objects.create(
                level='info',
                message=f'Cleansed {deleted} log entries older than 7 days',
                data={'cleansed_count': deleted},
                worker_id='celery_maintenance'
            )

        return f"Cleansed {deleted} old log entries"

    except Exception as e:
        logger.error("log_cleanse_failed", error=str(e))
        raise


# ============================================================================
# API KEY ROTATION
# ============================================================================

@shared_task
def rotate_expired_keys():
    """
    Daily task: Deactivate API keys past 90-day expiry.
    Security requirement from spec.
    """
    try:
        expired_keys = ProviderApiKey.objects.filter(
            is_active=True,
            expires_at__lt=timezone.now()
        )

        deactivated_count = 0
        for key in expired_keys:
            key.is_active = False
            key.save()
            deactivated_count += 1

            logger.warning("api_key_expired_deactivated",
                           key_name=key.name,
                           expired_at=key.expires_at.isoformat())

        if deactivated_count > 0:
            ProviderLog.objects.create(
                level='warning',
                message=f'Deactivated {deactivated_count} expired API keys',
                data={'deactivated_keys': [k.name for k in expired_keys]},
                worker_id='celery_security'
            )

        return f"Deactivated {deactivated_count} expired keys"

    except Exception as e:
        logger.error("key_rotation_failed", error=str(e))
        raise


# ============================================================================
# SYSTEM HEALTH CHECK
# ============================================================================

@shared_task
def system_health_check():
    """
    Periodic task: Check system health and alert on anomalies.
    Run every 5 minutes via Celery Beat.
    """
    alerts = []

    try:
        # Check 1: Queue buildup (more than 1000 pending jobs)
        pending_count = Job.objects.filter(status='pending').count()
        if pending_count > 1000:
            alerts.append(f"High pending job count: {pending_count}")
            logger.warning("high_pending_jobs", count=pending_count)

        # Check 2: Too many failed jobs (>10% of last 1000)
        recent_jobs = Job.objects.order_by('-enqueued_at')[:1000]
        if recent_jobs.count() > 100:
            failed_count = recent_jobs.filter(status='failed').count()
            fail_rate = failed_count / recent_jobs.count()
            if fail_rate > 0.1:
                alerts.append(f"High failure rate: {fail_rate:.1%}")
                logger.error("high_failure_rate", rate=fail_rate)

        # Check 3: No articles in last hour (scraper down?)
        one_hour_ago = timezone.now() - timedelta(hours=1)
        recent_articles = ProviderLog.objects.filter(
            timestamp__gte=one_hour_ago,
            message__contains='article_processed'
        ).count()

        if recent_articles == 0:
            # Check if we have pending jobs (if yes, workers might be stuck)
            if pending_count > 0:
                alerts.append("No articles processed in last hour despite pending jobs")
                logger.error("scraper_stalled", pending_jobs=pending_count)

        # Log if any alerts
        if alerts:
            ProviderLog.objects.create(
                level='critical' if len(alerts) > 2 else 'warning',
                message=f'System health check alerts: {"; ".join(alerts)}',
                data={
                    'alerts': alerts,
                    'pending_jobs': pending_count,
                    'timestamp': timezone.now().isoformat()
                },
                worker_id='celery_monitor'
            )
            return f"Health check alerts: {len(alerts)}"

        return "System health check passed"

    except Exception as e:
        logger.error("health_check_failed", error=str(e))
        raise


# ============================================================================
# SCHEDULE SCRAPING BATCHES
# ============================================================================

@shared_task
def trigger_scheduled_batch(force: bool = False):
    """
    Entry point for Celery Beat scheduled scraping.
    Creates batch job and queues RSS fetcher.
    """
    from modules.scrapers.rss_fetcher import fetch_and_en
