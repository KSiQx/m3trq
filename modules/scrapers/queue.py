"""
Redis-based queue abstraction with Render KV migration path.
Supports task queuing, results publishing, and deduplication.
"""
import os
import json
import redis
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()


class QueueManager:
    """
    Abstraction over Redis for task queues.
    Can be swapped to Render KV by changing connection logic.
    """

    def __init__(self):
        self.redis_url = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
        self.client = redis.from_url(self.redis_url, decode_responses=True)
        self.processed_set_key = "metrq:processed_urls"
        self.results_queue = "metrq:results_queue"
        self.scrape_queue = "metrq:scrape_queue"

    def push_scrape_task(self, url: str, priority: int = 1, metadata: Optional[Dict] = None) -> bool:
        """
        Push URL to scrape queue with priority.
        Returns False if already processed (deduplication).
        """
        # Check deduplication (48h TTL)
        if self.is_url_processed(url):
            logger.debug("url_deduplicated", url=url)
            return False

        task = {
            'url': url,
            'priority': priority,
            'metadata': metadata or {},
            'enqueued_at': datetime.now(timezone.utc).isoformat()
        }

        # Use priority queue (higher score = higher priority)
        score = 10 - priority  # Invert for Redis sorted set (lower = higher priority)
        self.client.zadd(self.scrape_queue, {json.dumps(task): score})

        logger.info("task_enqueued", url=url, priority=priority, queue="scrape")
        return True

    def pop_scrape_task(self, timeout: int = 5) -> Optional[Dict[str, Any]]:
        """
        Pop highest priority task from scrape queue.
        Blocking pop with timeout.
        """
        # Pop highest priority (lowest score)
        result = self.client.zpopmin(self.scrape_queue)

        if result:
            task_data = json.loads(result[0][0])
            logger.info("task_claimed", url=task_data['url'], queue="scrape")
            return task_data
        return None

    def mark_url_processed(self, url: str, ttl_hours: int = 48):
        """Mark URL as processed with TTL for deduplication."""
        self.client.setex(f"{self.processed_set_key}:{url}",
                          3600 * ttl_hours, "1")
        logger.debug("url_marked_processed", url=url, ttl_hours=ttl_hours)

    def is_url_processed(self, url: str) -> bool:
        """Check if URL was recently processed."""
        return self.client.exists(f"{self.processed_set_key}:{url}") == 1

    def push_result(self, job_id: str, article_data: Dict[str, Any]) -> bool:
        """
        Push processed article to results queue for DB Writer.
        This keeps workers stateless and respects single-writer pattern.
        """
        payload = {
            'job_id': job_id,
            'article': article_data,
            'completed_at': datetime.now(timezone.utc).isoformat()
        }

        self.client.lpush(self.results_queue, json.dumps(payload))
        logger.info("result_pushed", job_id=job_id, queue="results")
        return True

    def pop_result(self, timeout: int = 1) -> Optional[Dict[str, Any]]:
        """Pop result from queue (used by DB Writer)."""
        result = self.client.brpop(self.results_queue, timeout=timeout)
        if result:
            return json.loads(result[1])
        return None

    def get_queue_length(self, queue_name: str = "scrape") -> int:
        """Get current queue length for monitoring."""
        if queue_name == "scrape":
            return self.client.zcard(self.scrape_queue)
        elif queue_name == "results":
            return self.client.llen(self.results_queue)
        return 0


# Global instance
queue = QueueManager()
