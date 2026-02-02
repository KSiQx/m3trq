"""
MetrQ DB Writer Service - Stateless SQLite writer with Redis queue consumer.
Implements single-writer pattern with BEGIN IMMEDIATE concurrency control.
"""
import os
import sys
import json
import asyncio
import sqlite3
import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import redis.asyncio as redis
from dataclasses import dataclass

# Setup logging (JSON to stdout per spec)
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
    datefmt='%Y-%m-%dT%H:%M:%SZ'
)
logger = logging.getLogger('db_writer')


@dataclass
class ArticleMessage:
    """Structured article data from Redis queue."""
    job_id: str
    url: str
    title_origin: str
    title_translated: Optional[str]
    text_origin: Optional[str]
    text_translated: Optional[str]
    news_provider: str
    published_at: Optional[str]
    sentiment: Optional[float]
    bias: Optional[float]
    entities: Dict[str, Any]
    geotags: Dict[str, Any]
    language: str
    search_themes: str
    worker_id: str
    scraped_at: Optional[str] = None


class DBWriter:
    """
    Async SQLite writer with batch processing and exponential backoff.
    Consumes from Redis, writes to SQLite with UPSERT semantics.
    """

    def __init__(
            self,
            db_path: Optional[str] = None,
            redis_url: Optional[str] = None,
            batch_size: int = 10,
            max_retries: int = 5,
            lock_timeout_ms: int = 5000
    ):
        self.db_path = db_path or os.environ.get(
            'SQLITE_PATH',
            str(Path(__file__).parent.parent.parent / 'metrq_dj' / 'db.sqlite3')
        )
        self.redis_url = redis_url or os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.lock_timeout_ms = lock_timeout_ms

        self.redis: Optional[redis.Redis] = None
        self.processing_list = "metrq:results_queue:processing"
        self.source_queue = "metrq:results_queue"

    async def connect(self):
        """Initialize Redis connection."""
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        logger.info(f"DB Writer connected to Redis at {self.redis_url}")

    async def close(self):
        """Cleanup connections."""
        if self.redis:
            await self.redis.close()

    async def collect_batch(self, timeout: int = 5) -> List[Tuple[str, ArticleMessage]]:
        """
        Collect batch of messages from Redis using reliable queue pattern.
        Uses RPOPLPUSH to ensure messages aren't lost if writer crashes.
        Returns list of (redis_message_id, article_data) tuples.
        """
        batch = []

        for _ in range(self.batch_size):
            # RPOPLPUSH: atomically move from queue to processing list
            msg_json = await self.redis.brpoplpush(
                self.source_queue,
                self.processing_list,
                timeout=timeout if not batch else 1
            )

            if not msg_json:
                break

            try:
                data = json.loads(msg_json)
                article = ArticleMessage(
                    job_id=data['job_id'],
                    url=data['article']['url'],
                    title_origin=data['article']['title_origin'],
                    title_translated=data['article'].get('title_translated'),
                    text_origin=data['article'].get('text_origin'),
                    text_translated=data['article'].get('text_translated'),
                    news_provider=data['article']['news_provider'],
                    published_at=data['article'].get('published_at'),
                    sentiment=data['article'].get('sentiment'),
                    bias=data['article'].get('bias'),
                    entities=data['article'].get('entities', {}),
                    geotags=data['article'].get('geotags', {}),
                    language=data['article']['language'],
                    search_themes=data['article'].get('search_themes', ''),
                    worker_id=data['article']['worker_id'],
                    scraped_at=data['article'].get('scraped_at', datetime.now(timezone.utc).isoformat())
                )
                # Use message content as ID for removal later
                batch.append((msg_json, article))

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Invalid message format: {e}")
                # Remove from processing list and log
                await self.redis.lrem(self.processing_list, 1, msg_json)
                await self._log_provider_error("Invalid message format", str(e), data)

        return batch

    async def process_batch(self, batch: List[Tuple[str, ArticleMessage]]) -> bool:
        """
        Write batch to SQLite with BEGIN IMMEDIATE and UPSERT logic.
        Implements exponential backoff for database locked errors.
        """
        if not batch:
            return True

        messages, articles = zip(*batch)

        for attempt in range(self.max_retries):
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    # Critical: BEGIN IMMEDIATE for exclusive write lock
                    await db.execute('BEGIN IMMEDIATE')

                    try:
                        for article in articles:
                            await self._upsert_article(db, article)
                            await self._update_job_status(db, article.job_id)

                        await db.commit()

                        # Success: Remove from processing list
                        for msg in messages:
                            await self.redis.lrem(self.processing_list, 1, msg)

                        logger.info(f"Batch committed: {len(articles)} articles")
                        return True

                    except Exception as e:
                        await db.rollback()
                        raise

            except sqlite3.OperationalError as e:
                if 'database is locked' in str(e):
                    wait_time = 0.1 * (2 ** attempt)  # Exponential backoff: 0.1, 0.2, 0.4, 0.8, 1.6s
                    logger.warning(f"Database locked, retrying in {wait_time}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"SQLite operational error: {e}")
                    break
            except Exception as e:
                logger.error(f"Unexpected error during batch write: {e}")
                break

        # All retries exhausted - log to provider_logs and dead-letter
        logger.error(f"Failed to write batch after {self.max_retries} attempts")
        for article in articles:
            await self._log_provider_error(
                "Batch write failed",
                f"Max retries exceeded for job {article.job_id}",
                {'job_id': article.job_id, 'url': article.url}
            )
            # Remove from processing queue to prevent infinite loop
            # In production, move to dead-letter queue instead
            await self.redis.lrem(self.processing_list, 1, json.dumps({
                'job_id': article.job_id,
                'article': {
                    'url': article.url,
                    'title_origin': article.title_origin,
                    'news_provider': article.news_provider,
                    'language': article.language,
                    'worker_id': article.worker_id
                }
            }))

        return False

    async def _upsert_article(self, db: aiosqlite.Connection, article: ArticleMessage):
        """
        UPSERT article with conflict resolution on URL.
        Updates version counter and timestamps on conflict.
        """
        # Prepare JSON fields as TEXT for SQLite (PostgreSQL will use JSONB)
        entities_json = json.dumps(article.entities) if article.entities else '{}'
        geotags_json = json.dumps(article.geotags) if article.geotags else '{}'

        # Parse published_at
        published = article.published_at
        if published and 'T' in published:
            published = published.split('T')[0]  # YYYY-MM-DD format per schema

        sql = """
        INSERT INTO core_article (
            id, url, title_origin, title_translated, text_origin, text_translated,
            news_provider, published_at, sentiment, bias, entities, geotags,
            language, search_themes, worker_id, scraped_at, updated_at, version
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 1
        )
        ON CONFLICT(url) DO UPDATE SET
            updated_at = CURRENT_TIMESTAMP,
            version = version + 1,
            worker_id = excluded.worker_id,
            title_translated = COALESCE(excluded.title_translated, core_article.title_translated),
            text_translated = COALESCE(excluded.text_translated, core_article.text_translated),
            sentiment = COALESCE(excluded.sentiment, core_article.sentiment),
            bias = COALESCE(excluded.bias, core_article.bias),
            entities = excluded.entities,
            geotags = excluded.geotags
        WHERE excluded.worker_id != core_article.worker_id
        """

        await db.execute(sql, (
            article.job_id,  # Using job_id as article ID (or generate new UUID)
            article.url,
            article.title_origin,
            article.title_translated,
            article.text_origin,
            article.text_translated,
            article.news_provider,
            published,
            article.sentiment,
            article.bias,
            entities_json,
            geotags_json,
            article.language,
            article.search_themes,
            article.worker_id,
            article.scraped_at
        ))

    async def _update_job_status(self, db: aiosqlite.Connection, job_id: str):
        """Update job status to completed."""
        sql = """
        UPDATE core_job 
        SET status = 'completed', 
            completed_at = CURRENT_TIMESTAMP,
            locked_by = NULL,
            locked_at = NULL
        WHERE id = ?
        """
        await db.execute(sql, (job_id,))

    async def _log_provider_error(self, message: str, details: str, data: Dict):
        """Log error to provider_logs table for observability."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('BEGIN IMMEDIATE')
                await db.execute(
                    """INSERT INTO core_providerlog (timestamp, level, message, data, worker_id)
                       VALUES (CURRENT_TIMESTAMP, 'error', ?, ?, ?)""",
                    (message, json.dumps(data), 'db_writer')
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to log provider error: {e}")

    async def cleanup_stale_locks(self, timeout_minutes: int = 5):
        """
        Reset jobs stuck in 'processing' state (crashed workers).
        Should be run periodically via Celery beat.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).isoformat()

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('BEGIN IMMEDIATE')
                cursor = await db.execute(
                    """UPDATE core_job 
                       SET status = 'pending', locked_by = NULL, locked_at = NULL
                       WHERE status = 'processing' AND locked_at < ?
                       RETURNING id""",
                    (cutoff,)
                )
                rows = await cursor.fetchall()
                await db.commit()

                if rows:
                    logger.info(f"Cleanup: Reset {len(rows)} stale jobs")
                    return len(rows)
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
        return 0

    async def run_continuous(self):
        """Main loop: consume and write continuously."""
        await self.connect()
        logger.info("DB Writer started")

        try:
            while True:
                batch = await self.collect_batch()
                if batch:
                    await self.process_batch(batch)
                else:
                    await asyncio.sleep(1)  # Idle sleep
        except asyncio.CancelledError:
            logger.info("DB Writer shutting down...")
        finally:
            await self.close()


# Celery Integration Task
from celery import shared_task


@shared_task(bind=True, max_retries=3)
def run_db_writer_batch(self, batch_size: int = 10):
    """
    Celery task wrapper for DB Writer (for scheduled execution).
    Can be called via Celery Beat or manually.
    """

    async def _run():
        writer = DBWriter(batch_size=batch_size)
        await writer.connect()
        try:
            batch = await writer.collect_batch(timeout=10)
            if batch:
                success = await writer.process_batch(batch)
                if not success:
                    raise self.retry(countdown=60)
            return f"Processed {len(batch)} articles"
        finally:
            await writer.close()

    return asyncio.run(_run())


@shared_task
def cleanup_stale_job_locks():
    """Celery beat task to reset crashed jobs."""

    async def _cleanup():
        writer = DBWriter()
        count = await writer.cleanup_stale_locks()
        return f"Reset {count} stale locks"

    return asyncio.run(_cleanup())


# Standalone execution
if __name__ == "__main__":
    writer = DBWriter()
    try:
        asyncio.run(writer.run_continuous())
    except KeyboardInterrupt:
        print("Shutdown requested")
