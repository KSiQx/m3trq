# metrq_dj/api/routers/dashboard.py
import json
import logging
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional
from collections import defaultdict, Counter

from django.db.models import Avg, Count, Q
from django.db import transaction, connection, models
from django.http import HttpResponse
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.conf import settings

from ninja import Router, Schema, Query
from ninja.security import HttpBearer
from ninja.errors import HttpError

from core.models import Article, RateLimit, ProviderLog
from accounts.models import Profile

logger = logging.getLogger(__name__)

"""
KM MetrQ Dashboard API Endpoint - Tier-based access with optimized queries.
Extended with language-specific article endpoints and rate limit status.
"""

# All endpoints added to this router will be automatically tagged as part of the dashboard (the "Dashboard" tag).
router = Router(tags=["Dashboard"])


# ============================================================================
# AUTHENTICATION CUSTOM
# ============================================================================

class JWTAuth(HttpBearer):
    def authenticate(self, request, token: str):
        from ninja_jwt.tokens import AccessToken
        try:
            access_token = AccessToken(token)

            # We receive data directly from the token's Payload WITHOUT a database query
            user_id = access_token.get('user_id')
            token_tier = access_token.get('tier', 'free')
            # Fetch user from the database once to check rate limits
            user = User.objects.select_related('profile').get(id=user_id)
            # Populate request object with user and tier information
            request.user = user
            # User's tier (e.g., 'free', 'pro', 'enterprise') extracted from JWT payload
            # Use this to apply tier-specific logic in endpoints
            request.tier = token_tier

            return user_id
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return None


auth = JWTAuth()


# ============================================================================
# SCHEMAS
# ============================================================================

class ArticleSchema(Schema):
    """
    Schema for article data representation in API responses.
    Includes core article metadata: ID, provider, titles (original and translated),
    URL, sentiment score, and bias profile.
    """

    id: str
    news_provider: str
    title_translated: Optional[str]
    title_origin: str
    url: str
    sentiment: float
    article_bias_profile: float


class MetricsSchema(Schema):
    """
    Schema for dashboard metrics.
    Contains aggregated statistics: number of articles in the last 24 hours,
    average sentiment, top entities, and top persons by language.
    """

    articles_24h: int
    avg_sentiment: float
    top_entities: List[str]
    top_persons_by_language: Dict[str, List[str]]


class DashboardResponse(Schema):
    """
    Schema for the dashboard API response.
    Combines user tier information, aggregated metrics,recent articles grouped by language,
    and rate limit data.
    """

    tier: str
    metrics: MetricsSchema
    recent_articles: Dict[str, List[ArticleSchema]]
    rate_limit_used: int
    rate_limit_remaining: int
    rate_limit_limit: int


class ArticleListResponse(Schema):
    """
    Schema for paginated article list responses.
    Includes a list of articles, total count, current page, and total pages.
    """

    articles: List[ArticleSchema]
    total: int
    page: int
    pages: int


class ArticleFilterSchema(Schema):
    """
    Schema for article list filtering and pagination.
    Defaults to page 1 and 20 articles per page.
    """

    page: int = 1
    per_page: int = 20


class RateLimitStatusSchema(Schema):
    """
    Schema for rate limit status information.
    Includes user tier, request limits, current usage, remaining requests,
    and the time when the limit resets.
    """

    tier: str
    limit: int
    used: int
    remaining: int
    reset_at: datetime


class BiasExplanationSchema(Schema):
    """
    Schema for news provider bias explanation.
    Contains the provider name, average bias score, and a human-readable
    explanation of the bias level.
    """

    provider: str
    bias_score: float
    explanation: str


# ============================================================================
# DASHBOARD ENDPOINTS
# ============================================================================

@router.get("/", response=DashboardResponse, auth=auth)
def get_dashboard(request, response: HttpResponse):
    """
    Get user dashboard with metrics and recent articles.
    Implements tier-based rate limiting and optimized queries.
    """
    user = request.user
    profile = user.profile

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
                entities = json.loads(article.entities) if isinstance(article.entities,
                                                                      str) else article.entities
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

    # Get limits
    today = timezone.now().date()
    limit = settings.RATE_LIMITS.get(profile.tier, 50)
    rate_limit = RateLimit.objects.filter(user=user, request_date=today).first()
    used = rate_limit.request_count if rate_limit else 0
    remaining = float('inf') if limit == float('inf') else max(0, limit - used)

    # Headers Data from HTTP headers not Json
    response['X-RateLimit-Limit'] = str(int(limit)) if limit != float('inf') else '-1'
    response['X-RateLimit-Remaining'] = str(int(remaining)) if remaining != float('inf') else '-1'

    # Build response
    result = DashboardResponse(
        tier=profile.tier,
        metrics=MetricsSchema(
            articles_24h=articles_24h,
            avg_sentiment=avg_sentiment,
            top_entities=top_entities,
            top_persons_by_language=dict(persons_by_lang)
        ),
        recent_articles=recent_articles,
        rate_limit_used=used,
        rate_limit_remaining=int(remaining) if remaining != float('inf') else -1,
        rate_limit_limit=limit

    )

    return result


@router.get("/articles/{language}", response=ArticleListResponse, auth=auth)
def get_articles_by_language(
        request,
        language: str,
        filters: ArticleFilterSchema = Query(...)
):
    """
    Get paginated articles for a specific language.
    For "Read all recent articles" pages.
    """

    valid_languages = ['zh_cn', 'zh_tw', 'en', 'ru']
    if language not in valid_languages:
        raise HttpError(400, f"Invalid language. Must be one of: {', '.join(valid_languages)}")

    # Use select_related for performance if needed
    articles_qs = Article.objects.filter(
        language=language
    ).order_by('-scraped_at')

    paginator = Paginator(articles_qs, filters.per_page)
    page_obj = paginator.get_page(filters.page)

    response = ArticleListResponse(
        articles=[
            ArticleSchema(
                id=str(art.id),
                news_provider=art.news_provider,
                title_translated=art.title_translated or art.title_origin,
                title_origin=art.title_origin,
                url=art.url,
                sentiment=art.sentiment or 0.0,
                article_bias_profile=art.bias or 0.5
            ) for art in page_obj.object_list
        ],
        total=paginator.count,
        page=page_obj.number,
        pages=paginator.num_pages
    )

    return response


@router.get("/limits", response=RateLimitStatusSchema, auth=auth)
def get_rate_limit_status(request):
    """Get user's current rate limit status."""
    user = request.user
    profile = user.profile
    tier = profile.tier
    today = timezone.now().date()

    limit = settings.RATE_LIMITS.get(tier, 50)

    # Use filter().first() instead of try/except
    rate_limit = RateLimit.objects.filter(user=user, request_date=today).first()
    used = rate_limit.request_count if rate_limit else 0

    remaining = float('inf') if limit == float('inf') else max(0, limit - used)
    reset_at = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), datetime.min.time()),
    )
    # reset_at = datetime.combine(today + timedelta(days=1), datetime.min.time())
    # reset_at = reset_at.replace(tzinfo=timezone.utc)

    # A regular dictionary was used. Django Ninja will automatically package it into a RateLimitStatusSchema,
    # since is specified it in the response=... parameter.
    return {
        "tier": tier,
        "limit": int(limit) if limit != float('inf') else -1,
        "used": used,
        "remaining": int(remaining) if remaining != float('inf') else -1,
        "reset_at": reset_at
    }


@router.get("/providers/{provider_name}/bias", response=BiasExplanationSchema, auth=auth)
def get_provider_bias(request, provider_name: str):
    """
    Get bias explanation for a news provider.
    """

    # Calculate bias from articles
    articles = Article.objects.filter(news_provider=provider_name).exclude(
        bias__isnull=True
    )[:100]

    if not articles:
        raise HttpError(404, f"No data found for provider: {provider_name}")

    avg_bias = sum(a.bias or 0.5 for a in articles) / len(articles)

    # Generate explanation based on bias score
    if avg_bias < 0.3:
        explanation = f"{provider_name} shows minimal bias in coverage."
    elif avg_bias < 0.5:
        explanation = f"{provider_name} shows slight bias in topic selection."
    elif avg_bias < 0.7:
        explanation = f"{provider_name} shows moderate bias in framing."
    else:
        explanation = f"{provider_name} shows strong bias in coverage."

    return {
        "provider": provider_name,
        "bias_score": round(avg_bias, 3),
        "explanation": explanation
    }
