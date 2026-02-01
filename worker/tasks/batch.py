"""
Celery worker tasks with atomic job claiming from SQLite.
Stateless design: Claims from DB, Processes, Publishes to Redis.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path


# Ensure Django is set up for model access
worker_dir = Path(__file__).resolve().parent.parent
project_root = worker_dir.parent
metrq_dj_dir = project_root / 'metrq_dj'

sys.path.insert(0, str(metrq_dj_dir))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')
# sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'metrq_dj'))
# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')


import django


django.setup()

from django.db import transaction, connection
from django.utils import timezone
from celery import shared_task
from datetime import timedelta

from core.models import Job
from modules.scrapers.article_processor import ArticleProcessor
from modules.scrapers.queue import queue

import structlog

logger = structlog.get_logger()

WORKER_ID = os.environ.get('WORKER_ID', f"worker_{uuid.uuid4().hex[:8]}")


def claim_job_atomic() -> tuple:
    """
    Atomically claim a pending job using SQLite UPDATE...RETURNING.
    Falls back to select_for_update for PostgreSQL compatibility.
    """
    try:
        with transaction.atomic():
            # For SQLite 3.35+, use raw SQL UPDATE...RETURNING for atomic claim
            # This prevents race conditions without long-held locks
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute('BEGIN IMMEDIATE')
                    cursor.execute("""
                        UPDATE core_job 
                        SET status = 'processing', 
                            locked_by = %s, 
                            locked_at = CURRENT_TIMESTAMP 
                        WHERE id = (
                            SELECT id FROM core_job 
                            WHERE status = 'pending' 
                            ORDER BY priority DESC, enqueued_at ASC 
                            LIMIT 1
                        )
                        RETURNING id, url, retries, metadata
                    """, [WORKER_ID])

                    row = cursor.fetchone()
                    cursor.execute('COMMIT')

                    if row:
                        job_id, url, retries, metadata = row
                        return {'id': job_id, 'url': url, 'retries': retries, 'metadata': metadata or {}}
            else:
                # PostgreSQL path with select_for_update
                job = Job.objects.select_for_update(nowait=True, skip_locked=True).filter(
                    status='pending'
                ).order_by('-priority', 'enqueued_at').first()

                if job:
                    job.status = 'processing'
                    job.locked_by = WORKER_ID
                    job.locked_at = timezone.now()
                    job.save()
                    return {
                        'id': job.id,
                        'url': job.url,
                        'retries': job.retries,
                        'metadata': job.metadata or {}
                    }

    except Exception as e:
        logger.error("claim_job_failed", error=str(e), worker=WORKER_ID)

    return None


def release_job(job_id: str, success: bool, error_msg: str = None):
    """Update job status in SQLite."""
    try:
        job = Job.objects.get(id=job_id)

        if success:
            job.status = 'completed'
            job.completed_at = timezone.now()
        else:
            job.retries += 1
            if job.retries >= 5:
                job.status = 'failed'
                job.error_message = error_msg
            else:
                job.status = 'pending'  # Retry

        job.locked_by = None
        job.locked_at = None
        job.save()

    except Exception as e:
        logger.error("release_job_failed", job_id=job_id, error=str(e))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_single_article(self, job_data: dict = None):
    """
    Process a single article through the pipeline.
    If job_data provided, use it; otherwise claim from DB.
    """
    processor = ArticleProcessor(worker_id=WORKER_ID)

    # Claim job if not provided (for direct polling mode)
    if not job_data:
        job_data = claim_job_atomic()
        if not job_data:
            logger.debug("no_jobs_available", worker=WORKER_ID)
            return None

    job_id = job_data['id']
    url = job_data['url']

    logger.info("processing_job", job_id=str(job_id), url=url, worker=WORKER_ID)

    try:
        # Process article
        article_data = asyncio.run(processor.process(url, job_data.get('metadata', {})))

        if not article_data:
            release_job(job_id, success=False, error_msg="Processing returned None")
            # Retry logic
            if self.request.retries < 3:
                raise self.retry(countdown=60 * (2 ** self.request.retries))
            return None

        # Convert to dict and push to Redis (NOT to SQLite Articles table)
        article_dict = processor.to_dict(article_data)

        # Publish to results queue for DB Writer
        queue.push_result(
            job_id=str(job_id),
            article_data=article_dict
        )

        # Mark URL as processed in Redis (deduplication TTL)
        queue.mark_url_processed(url)

        # Release job as completed
        release_job(job_id, success=True)

        logger.info("job_completed", job_id=str(job_id), url=url)
        return article_dict

    except Exception as e:
        logger.error("job_failed", job_id=str(job_id), error=str(e))
        release_job(job_id, success=False, error_msg=str(e))
        raise self.retry(exc=e, countdown=60)


@shared_task
def cleanup_stale_jobs():
    """
    Reset jobs stuck in 'processing' state (worker crashes).
    Run via Celery Beat every 5 minutes.
    """
    timeout = timezone.now() - timedelta(minutes=5)

    stale_jobs = Job.objects.filter(
        status='processing',
        locked_at__lt=timeout
    )

    count = 0
    for job in stale_jobs:
        logger.warning("resetting_stale_job",
                       job_id=str(job.id),
                       locked_by=job.locked_by,
                       locked_at=job.locked_at.isoformat())
        job.status = 'pending'
        job.locked_by = None
        job.locked_at = None
        job.save()
        count += 1

    return f"Reset {count} stale jobs"


@shared_task
def batch_process_claim():
    """
    Continuously claim and process jobs until queue empty.
    Run this task repeatedly via Celery Beat or worker loop.
    """
    processed = 0

    while processed < 10:  # Process batch of 10 max per task invocation
        job = claim_job_atomic()
        if not job:
            break

        # Process synchronously for batch efficiency
        result = process_single_article.delay(job)  # Or run directly
        processed += 1

    return f"Queued {processed} jobs for processing"