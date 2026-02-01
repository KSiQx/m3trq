"""
RSS Feed fetcher with deduplication and backoff.
Populates scrape queue from configured sources.
"""
import os
import json
import time
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Set
from datetime import datetime, timedelta
from functools import wraps
import random

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urljoin

import structlog
from modules.scrapers.queue import queue

logger = structlog.get_logger()

# Config path from environment
# Use Django settings if available, fallback to os.environ
try:
    from django.conf import settings
    CONFIG_PATH = getattr(settings, 'CONFIG_PATH', os.environ.get('CONFIG_PATH', 'config/sources.json'))
except ImportError:
    from django.conf import settings
    # When running standalone (celery worker startup)
    CONFIG_PATH = os.environ.get('CONFIG_PATH', 'config/sources.json')
    if not os.path.isabs(CONFIG_PATH):
        # Try to resolve relative to current working directory or assume project structure
        project_root = Path(__file__).resolve().parent.parent.parent  # m3trq
        CONFIG_PATH = str(project_root / CONFIG_PATH)
# CONFIG_PATH = os.environ.get('CONFIG_PATH', 'config/sources.json')


class ExponentialBackoff:
    """Exponential backoff decorator for resilient fetching."""

    def __init__(self, max_retries=3, base_delay=1, max_delay=60):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(self.max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise

                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    # Add jitter
                    jitter = random.uniform(0, 0.1 * delay)
                    time.sleep(delay + jitter)
                    logger.warning("backoff_retry",
                                   attempt=attempt + 1,
                                   delay=delay,
                                   error=str(e),
                                   source=args[0] if args else "unknown")
            return None

        return wrapper


class RSSFetcher:
    """Fetches articles from RSS feeds with deduplication."""

    def __init__(self, sources_config_path: Optional[str] = None):
        self.sources_path = sources_config_path or CONFIG_PATH
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MetrQ-Bot/1.0 (News Analytics)'
        })
        # Retry on common errors
        adapter = HTTPAdapter(max_retries=3)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def load_sources(self) -> List[Dict]:
        """Load RSS sources from JSON config."""
        try:
            with open(self.sources_path, 'r') as f:
                config = json.load(f)
                return config.get('sources', [])
        except Exception as e:
            logger.error("config_load_failed", path=self.sources_path, error=str(e))
            # Fallback to default sources if file missing
            return self._default_sources()

    def _default_sources(self) -> List[Dict]:
        """Default sources for testing."""
        return [
            {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "lang": "en", "priority": 1},
            {"name": "Reuters", "url": "http://feeds.reuters.com/reuters/worldnews", "lang": "en", "priority": 1},
            # Add more defaults as needed for zh, ru
        ]

    @ExponentialBackoff(max_retries=3, base_delay=2)
    def fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """Fetch RSS feed with exponential backoff."""
        logger.info("fetching_feed", url=url)

        # Use feedparser for robust parsing
        parsed = feedparser.parse(url)

        if parsed.bozo and hasattr(parsed, 'bozo_exception'):
            logger.warning("feed_parse_warning", url=url, error=str(parsed.bozo_exception))

        if parsed.entries:
            return parsed
        return None

    def extract_article_urls(self, feed_data: feedparser.FeedParserDict,
                             source_lang: str,
                             source_name: str) -> List[Dict]:
        """Extract article metadata from feed."""
        articles = []

        for entry in feed_data.entries:
            # Handle relative URLs
            url = entry.get('link', '')
            if not url:
                continue

            title = entry.get('title', '')
            published = entry.get('published', '')

            # Skip if too old (7 days)
            if self._is_too_old(published):
                continue

            articles.append({
                'url': url,
                'title': title,
                'source': source_name,
                'language': source_lang,
                'published': published
            })

        return articles

    def _is_too_old(self, published: str, days: int = 7) -> bool:
        """Check if article is too old to process."""
        try:
            # Simple check - in production use parsed time
            #TODO: make Simple check - in production use parsed time
            return False  # Allow all for now, filter by DB later
        except:
            return False

    def run(self, target_count: int = 100) -> int:
        """
        Fetch all feeds and enqueue new articles.
        Returns number of articles enqueued.
        """
        sources = self.load_sources()
        enqueued = 0
        processed_urls: Set[str] = set()

        for source in sources:
            try:
                feed = self.fetch_feed(source['url'])
                if not feed:
                    continue

                articles = self.extract_article_urls(
                    feed,
                    source.get('lang', 'en'),
                    source.get('name', 'unknown')
                )

                for article in articles[:target_count // len(sources)]:
                    url = article['url']

                    # Skip duplicates within this batch
                    if url in processed_urls:
                        continue
                    processed_urls.add(url)

                    # Try to enqueue (returns False if deduplicated by Redis)
                    success = queue.push_scrape_task(
                        url=url,
                        priority=source.get('priority', 1),
                        metadata={
                            'source': article['source'],
                            'language': article['language'],
                            'original_title': article['title']
                        }
                    )

                    if success:
                        enqueued += 1
                        logger.info("article_enqueued",
                                    url=url,
                                    source=article['source'])

            except Exception as e:
                logger.error("feed_processing_failed",
                             source=source.get('name'),
                             error=str(e))
                continue

        logger.info("fetch_batch_complete",
                    enqueued_enqueued=enqueued,
                    sources_processed=len(sources))
        return enqueued


# Convenience function for Celery scheduling
def fetch_and_enqueue():
    """Celery task entry point for RSS fetching."""
    fetcher = RSSFetcher()
    return fetcher.run(target_count=50)  # Configurable batch size
