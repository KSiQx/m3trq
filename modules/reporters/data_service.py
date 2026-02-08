"""
MetrQ Report Data Service
Aggregates and prepares data for report generation.
Optimized for SQLite → PostgreSQL migration.
"""

import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict, Counter
from dataclasses import dataclass

from django.db import connection
from django.utils import timezone
from core.models import Article, Report
from accounts.models import Profile

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ReportMetrics:
    """Container for report metrics data."""
    articles_count: int
    avg_sentiment: float
    sentiment_timeline: List[Dict[str, Any]]
    top_entities: List[Dict[str, Any]]
    top_persons: Dict[str, List[str]]
    language_distribution: Dict[str, int]
    bias_distribution: Dict[str, int]
    media_breakdown: List[Dict[str, Any]]


class ReportDataService:
    """
    Service for aggregating report data from database.
    Async-compatible for future asyncpg migration.
    """

    def __init__(self):
        self.logger = logger.bind(service="ReportDataService")

    async def get_report_data(
        self,
        user_id: int,
        report_type: str,
        days: int = 7
    ) -> Dict[str, Any]:
        """Aggregate all data needed for a report."""
        if report_type == 'monthly':
            days = 30

        end_date = timezone.now()
        start_date = end_date - timedelta(days=days)

        self.logger.info(
            "aggregating_report_data",
            user_id=user_id,
            report_type=report_type,
            days=days
        )

        # Run all aggregations concurrently
        results = await asyncio.gather(
            self._get_article_metrics(start_date, end_date),
            self._get_sentiment_timeline(start_date, end_date),
            self._get_top_entities(start_date, end_date),
            self._get_top_persons(start_date, end_date),
            self._get_language_distribution(start_date, end_date),
            self._get_bias_distribution(start_date, end_date),
            self._get_media_breakdown(start_date, end_date),
            return_exceptions=True
        )

        return {
            'report_type': report_type,
            'date_range': {
                'start': start_date.isoformat(),
                'end': end_date.isoformat(),
                'days': days
            },
            'generated_at': timezone.now().isoformat(),
            'user_id': user_id,
            'metrics': results[0],
            'sentiment_timeline': results[1],
            'top_entities': results[2],
            'top_persons': results[3],
            'language_distribution': results[4],
            'bias_distribution': results[5],
            'media_breakdown': results[6]
        }

    async def _get_article_metrics(self, start_date, end_date):
        """Get basic article metrics."""
        def _query():
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*), AVG(sentiment)
                    FROM core_article
                    WHERE scraped_at >= %s AND scraped_at <= %s
                """, [start_date, end_date])
                row = cursor.fetchone()
                return {
                    'total_articles': row[0] or 0,
                    'avg_sentiment': round(row[1] or 0.0, 3)
                }
        return await asyncio.to_thread(_query)

    async def _get_sentiment_timeline(self, start_date, end_date):
        """Get daily sentiment averages."""
        def _query():
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT DATE(scraped_at), COUNT(*), AVG(sentiment)
                    FROM core_article
                    WHERE scraped_at >= %s AND scraped_at <= %s
                    GROUP BY DATE(scraped_at)
                    ORDER BY 1
                """, [start_date, end_date])
                return [{'date': row[0], 'count': row[1], 'avg_sentiment': round(row[2] or 0.0, 3)}
                        for row in cursor.fetchall()]
        return await asyncio.to_thread(_query)

    async def _get_top_entities(self, start_date, end_date, limit=20):
        """Extract and count entities."""
        def _query():
            articles = Article.objects.filter(
                scraped_at__gte=start_date,
                scraped_at__lte=end_date,
                entities__isnull=False
            ).values_list('entities', 'language')[:500]

            entity_counter = Counter()
            for entities_json, lang in articles:
                if not entities_json:
                    continue
                try:
                    entities = json.loads(entities_json) if isinstance(entities_json, str) else entities_json
                    for entity_type, items in entities.items():
                        if isinstance(items, list):
                            for item in items:
                                entity_counter[f"{entity_type}:{item}"] += 1
                except (json.JSONDecodeError, TypeError):
                    continue

            return [{'name': k.split(':', 1)[1], 'type': k.split(':', 1)[0], 'count': v}
                    for k, v in entity_counter.most_common(limit)]
        return await asyncio.to_thread(_query)

    async def _get_top_persons(self, start_date, end_date, limit_per_lang=10):
        """Get top persons by language."""
        def _query():
            articles = Article.objects.filter(
                scraped_at__gte=start_date,
                scraped_at__lte=end_date,
                entities__isnull=False
            ).values_list('entities', 'language')

            persons_by_lang = defaultdict(list)
            for entities_json, language in articles:
                if not entities_json:
                    continue
                try:
                    entities = json.loads(entities_json) if isinstance(entities_json, str) else entities_json
                    persons = entities.get('persons', [])
                    for person in persons:
                        if len(persons_by_lang[language]) < limit_per_lang and person not in persons_by_lang[language]:
                            persons_by_lang[language].append(person)
                except (json.JSONDecodeError, TypeError):
                    continue
            return dict(persons_by_lang)
        return await asyncio.to_thread(_query)

    async def _get_language_distribution(self, start_date, end_date):
        """Get article count by language."""
        def _query():
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT language, COUNT(*)
                    FROM core_article
                    WHERE scraped_at >= %s AND scraped_at <= %s
                    GROUP BY language
                """, [start_date, end_date])
                return {row[0]: row[1] for row in cursor.fetchall()}
        return await asyncio.to_thread(_query)

    async def _get_bias_distribution(self, start_date, end_date):
        """Get article count by bias category."""
        def _query():
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        CASE 
                            WHEN bias < 0.33 THEN 'left'
                            WHEN bias > 0.66 THEN 'right'
                            ELSE 'center'
                        END, COUNT(*)
                    FROM core_article
                    WHERE scraped_at >= %s AND scraped_at <= %s AND bias IS NOT NULL
                    GROUP BY 1
                """, [start_date, end_date])
                return {row[0]: row[1] for row in cursor.fetchall()}
        return await asyncio.to_thread(_query)

    async def _get_media_breakdown(self, start_date, end_date, limit=10):
        """Get breakdown by news provider."""
        def _query():
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT news_provider, COUNT(*), AVG(sentiment), AVG(bias)
                    FROM core_article
                    WHERE scraped_at >= %s AND scraped_at <= %s
                    GROUP BY news_provider
                    ORDER BY 2 DESC
                    LIMIT %s
                """, [start_date, end_date, limit])
                return [{'provider': row[0], 'count': row[1],
                        'avg_sentiment': round(row[2] or 0.0, 3),
                        'avg_bias': round(row[3] or 0.5, 3)}
                       for row in cursor.fetchall()]
        return await asyncio.to_thread(_query)


# Singleton instance
report_data_service = ReportDataService()