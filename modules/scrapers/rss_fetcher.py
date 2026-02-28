"""
RSS Fetcher - Multi-stage pipeline support with deduplication, backoff, and content filtering.
Populates scrape queue from configured sources with exclusion filtering.
"""
import os
import json
import time
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Set, Any
from datetime import datetime, timedelta
from functools import wraps
import random
import re
import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urljoin

import structlog
from modules.scrapers.queue import queue

logger = structlog.get_logger()

# Config path from environment
# Use Django settings if available, else fallback to os.environ
try:
    from django.conf import settings
    CONFIG_PATH = getattr(settings, 'CONFIG_PATH', os.environ.get('CONFIG_PATH', 'config/sources.json'))
    KEYWORDS_PATH = getattr(settings, 'KEYWORDS_PATH', os.environ.get('KEYWORDS_PATH', 'config/keywords.json'))
except ImportError:
    # When running standalone (celery worker startup)
    CONFIG_PATH = os.environ.get('CONFIG_PATH', 'config/sources.json')
    KEYWORDS_PATH = os.environ.get('KEYWORDS_PATH', 'config/keywords.json')
    if not os.path.isabs(CONFIG_PATH):
        project_root = Path(__file__).resolve().parent.parent.parent
        CONFIG_PATH = str(project_root / CONFIG_PATH)
        KEYWORDS_PATH = str(project_root / KEYWORDS_PATH)


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
                    jitter = random.uniform(0, 0.1 * delay)
                    time.sleep(delay + jitter)
                    logger.warning("backoff_retry",
                                   attempt=attempt + 1,
                                   delay=delay,
                                   error=str(e),
                                   source=args[0] if args else "unknown")
            return None

        return wrapper


class NewsFilter:
    """
    Utility class for filtering and processing news articles.

    Features:
    - Exclusion word filtering (skip articles with sports/entertainment keywords)
    - Text cleanup (remove filler phrases, ads, HTML entities)
    - Tag extraction (identify sub-topics within categories)
    - Graceful fallback if configuration is missing

    Usage:
        filter = NewsFilter()  # loads config/keywords.json
        if filter.has_exclusion_words(title + content, lang):
            logger.info(f"Article skipped: {title}")
            return
        cleaned_content = filter.cleanup_text(content)
        tags = filter.extract_tags(cleaned_content, lang, category)
    """

    _LANG_MAP = {
        # Simplified Chinese (China, Singapore, Malaysia)
        'zh': 'zh_cn',
        'zh-cn': 'zh_cn',
        'zh-hans': 'zh_cn',
        'zh-hans-cn': 'zh_cn',
        'zh-sg': 'zh_cn',
        'zh-hans-sg': 'zh_cn',
        'zh-my': 'zh_cn',

        # Traditional Chinese (Taiwan, Hong Kong, Macau)
        'zh-hant': 'zh_tw',
        'zh-hant-tw': 'zh_tw',
        'zh-tw': 'zh_tw',
        'zh-hk': 'zh_tw',
        'zh-hant-hk': 'zh_tw',
        'zh-mo': 'zh_tw',
        'zh-hant-mo': 'zh_tw',

        # English
        'en-us': 'en',
        'en-gb': 'en',
        'en-au': 'en',
        'en-sg': 'en',

        # Russian
        'ru-ru': 'ru',
    }

    def __init__(self, config_path: str):
        """
        Initialize NewsFilter with keyword configuration.

        Args:
            config_path: Path to keywords.json configuration file
        """
        if config_path is None:
            config_path = KEYWORDS_PATH
        self.config = self._load_config(config_path)
        self.exclusion_cache: Dict[str, List[str]] = {}
        self.tagging_cache: Dict[str, Dict] = {}
        self._build_caches()

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """
        Load configuration from JSON file with graceful fallback.

        Returns:
            Configuration dict or empty structure if file missing/invalid
        """
        default_config = {
            'filters': {
                'tagging': {'by_language': {}},
                'exclusion': {'by_language': {}},
                'cleanup': {'patterns': [], 'replacements': {}}
            }
        }

        try:
            path = Path(config_path)
            if not path.exists():
                # Try relative to project root
                path = Path(__file__).parent.parent.parent / config_path

            if not path.exists():
                logger.warning(f"Keywords config not found at {config_path}, using empty filters")
                return default_config

            with open(path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info(f"Loaded keyword config from {path}")
                return config

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in keywords config: {e}")
            return default_config
        except Exception as e:
            logger.error(f"Error loading keywords config: {e}")
            return default_config

    def _build_caches(self):
        """Pre-process keywords for faster lookup."""
        exclusions = self.config.get('filters', {}).get('exclusion', {}).get('by_language', {})
        for lang, categories in exclusions.items():
            all_words = []
            for cat_words in categories.values():
                all_words.extend([w.lower() for w in cat_words])
            self.exclusion_cache[lang] = all_words

        tagging = self.config.get('filters', {}).get('tagging', {}).get('by_language', {})
        self.tagging_cache = tagging

    def cleanup_text(self, text: str) -> str:
        """
        Clean text from filler phrases, ads, and HTML entities.
        Args:
            text: Raw article text
        Returns:
            Cleaned text
        """
        if not text:
            return ""

        cleanup_config = self.config.get('filters', {}).get('cleanup', {})

        # Apply regex patterns
        for pattern_info in cleanup_config.get('patterns', []):
            pattern = pattern_info.get('pattern', '')
            if not pattern:
                continue

            flags = 0
            if pattern_info.get('flags', '').lower() == 'i':
                flags = re.IGNORECASE

            try:
                text = re.sub(pattern, '', text, flags=flags)
            except re.error:
                continue

        # Apply replacements (HTML entities, multiple spaces, etc.)
        for replacement_info in cleanup_config.get('replacements', {}).values():
            pattern = replacement_info.get('pattern', '')
            replacement = replacement_info.get('replacement', '')
            if pattern:
                try:
                    text = re.sub(pattern, replacement, text)
                except re.error:
                    continue

        return text.strip()

    def has_exclusion_words(self, text: str, lang: str) -> bool:
        """
        Check if text contains exclusion words (sports, entertainment, etc.).
        Args:
            text: Article text to check
            lang: Language code (en, ru, zh_cn, zh_tw)
        Returns:
            True if article should be excluded
        """
        if not text:
            return False

        lang = self._normalize_lang(lang)
        exclusion_words = self.exclusion_cache.get(lang, [])
        if not exclusion_words:
            exclusion_words = self.exclusion_cache.get('en', [])

        if not exclusion_words:
            return False

        text_lower = text.lower()
        for word in exclusion_words:
            if word in text_lower:
                logger.debug(f"Exclusion word found: '{word}' in lang '{lang}'")
                return True

        return False

    def extract_tags(self, text: str, lang: str, category: str) -> List[str]:
        """
        Extract tags from text based on language and category.
        Args:
            text: Article text
            lang: Language code
            category: Source category (e.g., 'politics_domestic')
        Returns:
            List of matched tag names
        """
        if not text or not category:
            return []

        lang = self._normalize_lang(lang)
        text_lower = text.lower()
        tags = []

        lang_dict = self.tagging_cache.get(lang, {})
        category_dict = lang_dict.get(category, {})

        if not category_dict:
            lang_dict = self.tagging_cache.get('en', {})
            category_dict = lang_dict.get(category, {})

        for tag_name, keywords in category_dict.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    tags.append(tag_name)
                    break

        return tags

    def _normalize_lang(self, lang: str) -> str:
        """Normalize language code to config format."""
        # Safely handling None or empty strings
        if not lang:
            return 'en'  # Restore the default system language

        # Convert to lowercase once
        lang_lower = lang.lower()

        # Trying to find an exact match
        if lang_lower in self._LANG_MAP:
            return self._LANG_MAP[lang_lower]

        # Fallback logic for unknown dialects
        # For example, if 'zh-yue' (Cantonese) comes up, it's not in the dictionary.
        # We take the base part of the code before the hyphen.
        base_lang = lang_lower.split('-')[0]

        if base_lang == 'zh':
            return 'zh_cn'  # The default language for Chinese is Simplified.

        # For other languages, we return pure code (en -> en, fr -> fr)
        return base_lang

    def get_relevance_score(self, text: str, title: str, lang: str, category: str) -> float:
        """
        Calculate relevance score for an article.

        Args:
            text: Article body text
            title: Article title
            lang: Language code
            category: Source category

        Returns:
            Relevance score (higher = more relevant)
        """
        scoring_config = self.config.get('filters', {}).get('scoring', {})
        weights = scoring_config.get('weights', {
            'title_match': 3.0,
            'content_match': 1.0,
            'category_match': 2.0
        })

        score = 0.0

        # Extract tags from title (weighted higher)
        title_tags = self.extract_tags(title, lang, category)
        score += len(title_tags) * weights.get('title_match', 3.0)

        # Extract tags from content
        content_tags = self.extract_tags(text, lang, category)
        score += len(content_tags) * weights.get('content_match', 1.0)

        # Category match bonus
        if category in self.tagging_cache.get(self._normalize_lang(lang), {}):
            score += weights.get('category_match', 2.0)

        return score

    def should_process_article(self, title: str, content: str, lang: str, category: str = None) -> tuple[bool, str]:
        """
        Comprehensive check if article should be processed.

        Args:
            title: Article title
            content: Article content
            lang: Language code
            category: Optional source category

        Returns:
            Tuple of (should_process, reason)
        """
        # Check exclusion words
        if self.has_exclusion_words(title + " " + content, lang):
            return False, "exclusion_words"

        # Check minimum length
        if len(content) < 100:
            return False, "too_short"

        # Check relevance score if category provided
        if category:
            score = self.get_relevance_score(content, title, lang, category)
            thresholds = self.config.get('filters', {}).get('scoring', {}).get('thresholds', {})
            min_score = thresholds.get('minimum_relevance_score', 0)

            if score < min_score:
                return False, "low_relevance"

        return True, "ok"


class RSSFetcher:
    """Fetches articles from RSS feeds with deduplication and filtering."""

    def __init__(self, sources_config_path: Optional[str] = None, keywords_config_path: Optional[str] = None):
        self.sources_path = sources_config_path or CONFIG_PATH
        self.keywords_path = keywords_config_path or KEYWORDS_PATH
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'MetrQ-Bot/1.0 (News Analytics)'
        })
        # Retry on common errors
        adapter = HTTPAdapter(max_retries=3)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        # Initialize news filter for exclusion and cleanup
        self.news_filter = NewsFilter(self.keywords_path)

    def load_sources(self) -> List[Dict]:
        """Load RSS sources from JSON config with categories."""
        try:
            with open(self.sources_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                sources = config.get('sources', [])
                logger.info(f"Loaded {len(sources)} sources from {self.sources_path}")
                return sources
        except Exception as e:
            logger.error("config_load_failed", path=self.sources_path, error=str(e))
            return self._default_sources()

    def _default_sources(self) -> List[Dict]:
        """Default sources for testing with categories."""
        return [
            {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml",
             "lang": "en", "priority": 1, "category": "politics_global"},
            {"name": "Reuters", "url": "http://feeds.reuters.com/reuters/worldnews",
             "lang": "en", "priority": 1, "category": "politics_global"},
            {"name": "新华", "url": "http://www.xinhuanet.com/english/rss/worldrss.xml",
             "lang": "zh_cn", "priority": 1, "category": "politics_domestic"},
            {"name": "ТАСС", "url": "http://tass.com/rss/v2.xml",
             "lang": "ru", "priority": 2, "category": "politics_domestic"},
        ]

    @ExponentialBackoff(max_retries=3, base_delay=2)
    def fetch_feed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """Fetch RSS feed with exponential backoff."""
        logger.info("fetching_feed", url=url)

        try:
            # 1. Use self.session (supports User-Agent and retry at the TCP level)
            # This is better than feedparser.parse(url) because it gives you control over headers.
            response = self.session.get(url, timeout=10)

            # 2. Checking the HTTP status.
            # # If the code is 4xx or 5xx, throw requests.exceptions.HTTPError
            response.raise_for_status()

            # 3. Parse the already received content (bytes)
            parsed = feedparser.parse(response.content)

            # 4. Checking for feed structure errors (bozo)
            if parsed.bozo and hasattr(parsed, 'bozo_exception'):
                logger.warning("feed_parse_warning",
                               url=url,
                               error=str(parsed.bozo_exception))

            if parsed.entries:
                return parsed
            return None

        except requests.exceptions.HTTPError as e:
            # We log a specific HTTP error (404, 500, 403)
            status_code = e.response.status_code
            logger.error("http_error",
                         url=url,
                         status_code=status_code,
                         reason=e.response.reason,
                         error=str(e))
            raise  # Rethrow the exception so that the ExponentialBackoff decorator works.
        except requests.exceptions.RequestException as e:
            # Network errors (DNS, Connection refused, Timeout)
            logger.error("network_error", url=url, error=str(e))
            raise


    def extract_article_urls(self, feed_data: feedparser.FeedParserDict,
                             source_lang: str,
                             source_name: str,
                             source_category: str) -> List[Dict]:
        """Extract article metadata from feed with category."""
        articles = []

        # We get the base URL of the site from the feed metadata (usually this is the home page)
        base_url = feed_data.feed.get('link', '')

        for entry in feed_data.entries:
            raw_link = entry.get('link', '')
            if not raw_link:
                continue

            # Convert a relative URL to absolute
            # If raw_link is already absolute, urljoin will return it as is.
            url = urljoin(base_url, raw_link)
            # Additional validation check (sometimes the URL may be broken, for example 'javascript:...')
            if not url.startswith(('http://', 'https://')):
                logger.warning("invalid_url_scheme", url=url, source=source_name)
                continue

            title = entry.get('title', '')
            published = entry.get('published', '')
            summary = entry.get('summary', '')

            # Skip if too old (7 days)
            if self._is_too_old(published):
                continue

            articles.append({
                'url': url,
                'title': title,
                'summary': summary,
                'source': source_name,
                'language': source_lang,
                'category': source_category,
                'published': published
            })

        return articles

    def _is_too_old(self, published: str, days: int = 7) -> bool:
        """Check if article is too old to process."""
        try:
            # Simple check - allow all for now, filter by DB later
            return False
        except:
            return False

    def run(self, target_count: int = 100) -> int:
        """
        Fetch all feeds and enqueue new articles with filtering.
        Returns number of articles enqueued.
        """
        sources = self.load_sources()
        enqueued = 0
        skipped_exclusion = 0
        processed_urls: Set[str] = set()

        for source in sources:
            try:
                feed = self.fetch_feed(source['url'])
                if not feed:
                    continue

                articles = self.extract_article_urls(
                    feed,
                    source.get('lang', 'en'),
                    source.get('name', 'unknown'),
                    source.get('category', 'politics_global')  # Get category from source
                )

                for article in articles[:target_count // len(sources)]:
                    url = article['url']

                    # Skip duplicates within this batch
                    if url in processed_urls:
                        continue
                    processed_urls.add(url)

                    # Apply exclusion filtering
                    content_to_check = article['title'] + " " + article.get('summary', '')
                    if self.news_filter.has_exclusion_words(content_to_check, article['language']):
                        logger.info("article_excluded_by_filter",
                                    url=url,
                                    source=article['source'],
                                    reason="exclusion_words")
                        skipped_exclusion += 1
                        continue

                    # Clean the summary/text
                    cleaned_summary = self.news_filter.cleanup_text(article.get('summary', ''))

                    # Try to enqueue (returns False if deduplicated by Redis)
                    success = queue.push_scrape_task(
                        url=url,
                        priority=source.get('priority', 1),
                        metadata={
                            'source': article['source'],
                            'language': article['language'],
                            'category': article['category'],  # Pass category to metadata
                            'original_title': article['title'],
                            'cleaned_summary': cleaned_summary,
                            'published': article['published']
                        }
                    )

                    if success:
                        enqueued += 1
                        logger.info("article_enqueued",
                                    url=url,
                                    source=article['source'],
                                    category=article['category'])

            except Exception as e:
                logger.error("feed_processing_failed",
                             source=source.get('name'),
                             error=str(e))
                continue

        logger.info("fetch_batch_complete",
                    enqueued=enqueued,
                    skipped_exclusion=skipped_exclusion,
                    sources_processed=len(sources))
        return enqueued


# Convenience function for quick filtering
def filter_article(title: str, content: str, lang: str, category: str = None, config_path: str = 'config/keywords.json') -> tuple[bool, str, str]:
    """
    Quick filter function for articles.

    Returns:
        Tuple of (should_process, reason, cleaned_content)
    """
    news_filter = NewsFilter(config_path)

    should_process, reason = news_filter.should_process_article(title, content, lang, category)

    if not should_process:
        return False, reason, ""

    cleaned = news_filter.cleanup_text(content)
    return True, "ok", cleaned


# Convenience function for Celery scheduling
def fetch_and_enqueue():
    """Celery task entry point for RSS fetching."""
    fetcher = RSSFetcher()
    return fetcher.run(target_count=50)  # Configurable batch size

