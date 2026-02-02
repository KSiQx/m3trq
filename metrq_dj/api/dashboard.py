"""
MetrQ Dashboard API Endpoint - Tier-based access with optimized queries.
"""
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict, Counter

from ninja import Router, Schema
from ninja.security import HttpBearer
from django.db import transaction, connection
from django.utils import timezone
from django.contrib.auth.models import User

from core.models import Article, RateLimit
from accounts.models import Profile

router = Router(tags=["Dashboard"])


# Auth class (assuming ninja_jwt)
class JWTAuth(HttpBearer):
    def authenticate(self, request, token: str):
        from ninja_jwt.tokens import AccessToken
        try:
            access_token = AccessToken(token)
            user_id = access_token.get('user_id')
            request.user = User.objects.select_related('profile').get(id=user_id)
            request.auth = access_token
            return user_id
        except Exception:
            return None


auth = JWTAuth()


# Response Schemas (matching spec exactly)
class ArticleSchema(Schema):
    id: str
    news_provider: str
    title_translated: Optional[str]
    title_origin: str
    url: str
    sentiment: float
    article_bias_profile: float


class MetricsSchema(Schema):
    articles_24h: int
    avg_sentiment: float
    top_entities: List[str]
    top_persons_by_language: Dict[str, List[str]]


class DashboardResponse(Schema):
    tier: str
    metrics: MetricsSchema
    recent_articles: Dict[str, List[ArticleSchema]]


class RateLimitMiddleware:
    """Handles tier-based rate limiting with atomic updates."""

    LIMITS = {
        'free': 50,
        'pro': 5000,
        'enterprise': float('inf')
    }

    @staticmethod
    def check_and_increment(user: User) -> tuple[bool, int, int, datetime]:
        """
        Atomic rate limit check using UPSERT pattern.
        Returns: (allowed, limit, remaining, reset_time)
        """
        tier = getattr(user.profile, 'tier', 'free')
        limit = RateLimitMiddleware.LIMITS.get(tier, 50)
        today = timezone.now().date()
        reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
        reset_time = reset_time.replace(tzinfo=timezone.utc)

        if tier == 'enterprise':
            return True, -1, -1, reset_time

        try:
            with transaction.atomic():
                # Use get_or_create with select_for_update for atomicity
                # In SQLite: BEGIN IMMEDIATE ensures exclusive lock
                if connection.vendor == 'sqlite':
                    with connection.cursor() as cursor:
                        cursor.execute('BEGIN IMMEDIATE')

                        # Try to update existing
                        cursor.execute(
                            """UPDATE core_ratelimit 
                               SET request_count = request_count + 1
                               WHERE user_id = %s AND request_date = %s
                               RETURNING request_count""",
                            [user.id, today]
                        )
                        row = cursor.fetchone()

                        if row:
                            count = row[0]
                            cursor.execute('COMMIT')
                        else:
                            # Insert new
                            cursor.execute(
                                """INSERT INTO core_ratelimit (user_id, request_date, request_count)
                                   VALUES (%s, %s, 1)""",
                                [user.id, today]
                            )
                            cursor.execute('COMMIT')
                            count = 1

                        remaining = limit - count
                        return count <= limit, limit, max(0, remaining), reset_time
                else:
                    # PostgreSQL path with Django ORM
                    rate_limit, created = RateLimit.objects.select_for_update().get_or_create(
                        user=user,
                        request_date=today,
                        defaults={'request_count': 0}
                    )

                    if not created and rate_limit.request_count >= limit:
                        return False, limit, 0, reset_time

                    rate_limit.request_count += 1
                    rate_limit.save()

                    remaining = limit - rate_limit.request_count
                    return True, limit, max(0, remaining), reset_time

        except Exception as e:
            # Fail open (allow request) but log error
            import logging
            logging.error(f"Rate limit check failed: {e}")
            return True, limit, 1, reset_time


@router.get("/", response=DashboardResponse, auth=auth)
def get_dashboard(request):
    """
    Get user dashboard with metrics and recent articles.
    Implements tier-based rate limiting and optimized queries.
    """
    user = request.user
    profile = user.profile

    # Check rate limits
    allowed, limit, remaining, reset_time = RateLimitMiddleware.check_and_increment(user)

    if not allowed:
        from ninja import HttpError
        raise HttpError(429, {
            "error": "Rate limit exceeded",
            "tier": profile.tier,
            "limit": limit,
            "retry_after": int((reset_time - timezone.now()).total_seconds()),
            "upgrade_url": "https://metrq.onrender.com/upgrade"
        })

    # Time range for queries (last 24h)
    time_24h_ago = timezone.now() - timedelta(hours=24)

    # Query 1: Articles count and avg sentiment (indexed query)
    stats = Article.objects.filter(
        scraped_at__gte=time_24h_ago
    ).aggregate(
        count=models.Count('id'),
        avg_sentiment=models.Avg('sentiment')
    )

    articles_24h = stats['count'] or 0
    avg_sentiment = round(stats['avg_sentiment'] or 0.0, 3)

    # Query 2: Recent articles with entities (for top entities extraction)
    # Uses idx_articles_scraped index
    recent_qs = Article.objects.filter(
        scraped_at__gte=time_24h_ago
    ).order_by('-scraped_at')[:200]  # Sample last 200 for entity analysis

    # Extract entities (JSON parsing in Python for SQLite compatibility)
    entity_counter = Counter()
    persons_by_lang = defaultdict(list)

    for article in recent_qs:
        if article.entities:
            try:
                entities = json.loads(article.entities) if isinstance(article.entities, str) else article.entities
                # Count all entity types
                for entity_type, items in entities.items():
                    if isinstance(items, list):
                        for item in items:
                            entity_counter[f"{entity_type}:{item}"] += 1

                # Extract persons by language (limit 10 per language)
                persons = entities.get('persons', [])
                for person in persons[:5]:  # Top 5 per article
                    if len(persons_by_lang[article.language]) < 10:
                        if person not in persons_by_lang[article.language]:
                            persons_by_lang[article.language].append(person)
            except (json.JSONDecodeError, TypeError):
                continue

    # Get top 10 entities overall
    top_entities = [item.split(':', 1)[1] for item, _ in entity_counter.most_common(10)]

    # Query 3: Recent articles per language (9 each)
    # Uses idx_articles_language_published composite index
    languages = ['zh_cn', 'zh_tw', 'en', 'ru']
    recent_articles = {}

    for lang in languages:
        articles = Article.objects.filter(
            language=lang
        ).order_by('-scraped_at')[:9]

        recent_articles[lang] = [
            ArticleSchema(
                id=str(art.id),
                news_provider=art.news_provider,
                title_translated=art.title_translated or art.title_origin,
                title_origin=art.title_origin,
                url=art.url,
                sentiment=art.sentiment or 0.0,
                article_bias_profile=art.bias or 0.5
            ) for art in articles
        ]

    # Build response
    response = DashboardResponse(
        tier=profile.tier,
        metrics=MetricsSchema(
            articles_24h=articles_24h,
            avg_sentiment=avg_sentiment,
            top_entities=top_entities,
            top_persons_by_language=dict(persons_by_lang)
        ),
        recent_articles=recent_articles
    )

    # Add rate limit headers
    from django.http import JsonResponse
    http_response = JsonResponse(response.dict())
    http_response['X-RateLimit-Limit'] = str(limit) if limit != -1 else 'unlimited'
    http_response['X-RateLimit-Remaining'] = str(remaining) if remaining != -1 else 'unlimited'
    http_response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))
    http_response['Access-Control-Allow-Origin'] = 'https://metrq.onrender.com'

    return http_response
