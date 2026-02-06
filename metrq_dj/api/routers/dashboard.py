import json
import logging
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional
from collections import defaultdict, Counter

from django.db.models import Avg, Count, Q
from django.db import transaction, connection, models
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.conf import settings


from ninja import Router, Schema, Query
from ninja.security import HttpBearer
from ninja.errors import HttpError

from core.models import Article, RateLimit, ProviderLog
from accounts.models import Profile

"""
KM MetrQ Dashboard API Endpoint - Tier-based access with optimized queries.
Extended with language-specific article endpoints and rate limit status.
"""

# TODO: Check if Rate Limit Headers Missing (Critical): The spec requires X-RateLimit-* headers on ALL authenticated responses. Currently only error responses have them
# TODO: New Endpoints Missing Rate Limiting:
#     /articles/{language} - should check rate limits
#     /limits - probably shouldn't consume quota (it's a status check)
# TODO: CORS Headers Missing: Access-Control-Allow-Origin: https://metrq.onrender.com not set


# All endpoints added to this router will be automatically tagged as part of the dashboard (the "Dashboard" tag).
router = Router(tags=["Dashboard"])


# ============================================================================
# AUTHENTICATION CUSTOM
# ============================================================================

# metrq_dj/api/routers/dashboard.py

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
            # Check rate limits and update request count atomically
            allowed, limit, remaining, reset_time = RateLimitMiddleware.check_and_increment(user)

            if not allowed:
                raise HttpError(429, "Rate limit exceeded")

            # Populate request object with user and tier information
            request.user = user
            # User's tier (e.g., 'free', 'pro', 'enterprise') extracted from JWT payload
            # Use this to apply tier-specific logic in endpoints
            request.tier = token_tier
            # Rate limit information for the current user
            # Contains:
            #   - remaining: Number of requests left in the current period
            #   - reset: Datetime when the rate limit will reset (UTC)
            # TODO: Use this to set X-RateLimit-* headers or inform the user about their limits
            request.rate_limit_info = {
                'remaining': remaining,
                'reset': reset_time
            }

            return user_id
        except Exception:
            logging.error(f"Authentication failed: {e}")
            return None


# class JWTAuth(HttpBearer):
#     """JWT Bearer token authentication for users."""
#
#     def authenticate(self, request, token: str):
#         from ninja_jwt.tokens import AccessToken
#         try:
#             access_token = AccessToken(token)
#             user_id = access_token.get('user_id')
#             request.user = User.objects.select_related('profile').get(id=user_id)
#             request.auth = access_token
#             request.tier = getattr(request.user.profile, 'tier', 'free')
#             return user_id
#         except Exception:
#             return None


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
    Combines user tier information, aggregated metrics, and recent articles
    grouped by language.
    """

    tier: str
    metrics: MetricsSchema
    recent_articles: Dict[str, List[ArticleSchema]]


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
# RATE LIMITING
# ============================================================================

class RateLimitMiddleware:
    """Handles tier-based rate limiting with atomic updates."""

    LIMITS = settings.RATE_LIMITS  # USE RATE_LIMITS from settings.py
    # LIMITS = {
    #     'free': 50,
    #     'pro': 5000,
    #     'enterprise': float('inf')
    # }

    @staticmethod
    def check_and_increment(user: User) -> tuple[bool, int, int, datetime]:
        """
        Atomic rate limit check using UPSERT pattern.
        Returns: (allowed, limit, remaining, reset_time)
        """
        tier = getattr(user.profile, 'tier', 'free')
        limit = RateLimitMiddleware.LIMITS.get(tier, 50)
        # Get current time in UTC (aware datetime)
        now = timezone.now()  # Returns UTC aware datetime because USE_TZ=True and TIME_ZONE='UTC'
        today = now.date()  # This is a naive date object (dates don't have timezones)
        # Create reset time as UTC aware datetime
        # Midnight UTC of next day (when rate limits reset)
        reset_time = timezone.make_aware(
            datetime.combine(today + timedelta(days=1), datetime.min.time()),
            timezone=timezone.utc
        )  # USE_TZ = True
        # reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
        # reset_time = reset_time.replace(tzinfo=timezone.utc)

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


# ============================================================================
# DASHBOARD ENDPOINTS
# ============================================================================

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

    # Add rate limit headers via Django response
    http_response = JsonResponse(response.dict())
    http_response['X-RateLimit-Limit'] = str(limit) if limit != -1 else 'unlimited'
    http_response['X-RateLimit-Remaining'] = str(remaining) if remaining != -1 else 'unlimited'
    http_response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))
    http_response['Access-Control-Allow-Origin'] = 'https://metrq.onrender.com'
    return http_response


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
    # Check rate limits
    allowed, limit, remaining, reset_time = RateLimitMiddleware.check_and_increment(request.user)

    if not allowed:
        raise HttpError(429, {
            "error": "Rate limit exceeded",
            "tier": request.user.profile.tier,
            "limit": limit,
            "retry_after": int((reset_time - timezone.now()).total_seconds()),
            "upgrade_url": "https://metrq.onrender.com/upgrade"
        })

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

    http_response = JsonResponse(response.dict())
    http_response['X-RateLimit-Limit'] = str(limit) if limit != -1 else 'unlimited'
    http_response['X-RateLimit-Remaining'] = str(remaining) if remaining != -1 else 'unlimited'
    http_response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))
    http_response['Access-Control-Allow-Origin'] = 'https://metrq.onrender.com'

    return http_response


@router.get("/limits", response=RateLimitStatusSchema, auth=auth)
def get_rate_limit_status(request):
    """Get user's current rate limit status."""
    user = request.user
    profile = user.profile
    tier = profile.tier
    today = timezone.now().date()

    limit = RateLimitMiddleware.LIMITS.get(tier, 50)


    # Use filter().first() instead of try/except
    rate_limit = RateLimit.objects.filter(user=user, request_date=today).first()
    used = rate_limit.request_count if rate_limit else 0
    # try:
    #     rate_limit = RateLimit.objects.get(user=user, request_date=today)
    #     used = rate_limit.request_count
    # except RateLimit.DoesNotExist:
    #     used = 0

    remaining = float('inf') if limit == float('inf') else max(0, limit - used)
    reset_at = timezone.make_aware(
        datetime.combine(today + timedelta(days=1), datetime.min.time()),
        timezone=timezone.utc
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
    # return RateLimitStatusSchema(
    #     tier=tier,
    #     limit=int(limit) if limit != float('inf') else -1,
    #     used=used,
    #     remaining=int(remaining) if remaining != float('inf') else -1,
    #     reset_at=reset_at
    # )


@router.get("/providers/{provider_name}/bias", response=BiasExplanationSchema, auth=auth)
def get_provider_bias(request, provider_name: str):
    """
    Get bias explanation for a news provider.
    Public endpoint - no authentication required.
    """
    # Checking n writing off limit
    allowed, limit, remaining, reset_time = RateLimitMiddleware.check_and_increment(request.user)

    if not allowed:
        raise HttpError(429, "Rate limit exceeded. Try again tomorrow.")

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
    # return BiasExplanationSchema(
    #     provider=provider_name,
    #     bias_score=round(avg_bias, 3),
    #     explanation=explanation
    # )




# """
# DS Dashboard API with tier-based access, rate limiting and optimized queries.
# Compatible with existing ninja_jwt configuration.
# """
#
# # Creating a router WITHOUT global authentication
# router = Router(tags=["Dashboard"])
#
#
# # ============================================================================
# # SCHEMAS
# # ============================================================================
#
# class ArticleSchema(Schema):
#     id: str
#     news_provider: str
#     title_translated: Optional[str]
#     title_origin: str
#     url: str
#     sentiment: float
#     article_bias_profile: float
#
#
# class MetricsSchema(Schema):
#     articles_24h: int
#     avg_sentiment: float
#     top_entities: List[str]
#     top_persons_by_language: Dict[str, List[str]]
#
#
# class DashboardResponse(Schema):
#     tier: str
#     metrics: MetricsSchema
#     recent_articles: Dict[str, List[ArticleSchema]]
#
#
# class ArticleListResponse(Schema):
#     articles: List[ArticleSchema]
#     total: int
#     page: int
#     pages: int
#
#
# class ArticleFilterSchema(Schema):
#     page: int = 1
#     per_page: int = 20
#
#
# class RateLimitStatusSchema(Schema):
#     tier: str
#     limit: int
#     used: int
#     remaining: int
#     reset_at: datetime
#
#
# class BiasExplanationSchema(Schema):
#     provider: str
#     bias_score: float
#     explanation: str
#
#
# # ============================================================================
# # AUXILIARY FUNCTIONS (adapted to current settings)
# # ============================================================================
#
# def get_user_tier(user: User) -> str:
#     """Getting a user's tier from a profile"""
#     try:
#         return user.profile.tier
#     except Profile.DoesNotExist:
#         return 'free'
#
#
# def check_rate_limit(user: User) -> tuple[bool, int, int, datetime]:
#     """
#     Проверка rate limit с использованием вашей существующей модели RateLimit.
#     Возвращает: (allowed, limit, remaining, reset_time)
#     """
#     from metrq_site import settings
#
#     tier = get_user_tier(user)
#
#     # We use the limits from my settings.py
#     limit = settings.RATE_LIMITS.get(tier, 50)
#
#     if tier == 'enterprise' or limit == float('inf'):
#         return True, -1, -1, timezone.now() + timedelta(days=1)
#
#     today = timezone.now().date()
#
#     # Using atomic update_or_create
#     rate_limit, created = RateLimit.objects.select_for_update().get_or_create(
#         user=user,
#         request_date=today,
#         defaults={'request_count': 1}
#     )
#
#     if not created:
#         if rate_limit.request_count >= limit:
#             return False, limit, 0, datetime.combine(
#                 today + timedelta(days=1), datetime.min.time()
#             ).replace(tzinfo=timezone.utc)
#
#         rate_limit.request_count += 1
#         rate_limit.save()
#
#     remaining = limit - rate_limit.request_count
#     reset_time = datetime.combine(
#         today + timedelta(days=1), datetime.min.time()
#     ).replace(tzinfo=timezone.utc)
#
#     return True, limit, max(0, remaining), reset_time
#
#
# # ============================================================================
# # DASHBOARD ENDPOINTS (используем ваш существующий JWTAuth)
# # ============================================================================
#
# @router.get("/", response=DashboardResponse, auth=JWTAuth())
# def get_dashboard(request):
#     """
#     Get user dashboard with metrics and recent articles.
#     Использует существующую JWT аутентификацию и вашу конфигурацию.
#     """
#     user = request.user
#
#     # Проверяем rate limit (опционально - если хотите дублировать middleware)
#     allowed, limit, remaining, reset_time = check_rate_limit(user)
#
#     if not allowed:
#         raise HttpError(429, {
#             "error": "Rate limit exceeded",
#             "tier": get_user_tier(user),
#             "limit": limit,
#             "retry_after": int((reset_time - timezone.now()).total_seconds()),
#             "upgrade_url": "https://metrq.onrender.com/upgrade"
#         })
#
#     # ВАШ СУЩЕСТВУЮЩИЙ КОД для получения данных (адаптированный)
#     profile = user.profile
#     last_24h = timezone.now() - timedelta(hours=24)
#
#     # Metrics calculation (оптимизированная версия)
#     articles_24h = Article.objects.filter(
#         scraped_at__gte=last_24h
#     ).count()
#
#     avg_sentiment_qs = Article.objects.filter(
#         scraped_at__gte=last_24h,
#         sentiment__isnull=False
#     ).aggregate(avg=models.Avg('sentiment'))
#
#     avg_sentiment = round(avg_sentiment_qs['avg'] or 0, 3)
#
#     # Top entities
#     recent_articles_qs = Article.objects.filter(
#         scraped_at__gte=last_24h,
#         entities__isnull=False
#     ).order_by('-scraped_at')[:100]
#
#     entity_counts = {}
#     persons_by_lang = {
#         'zh_cn': [],
#         'zh_tw': [],
#         'en': [],
#         'ru': []
#     }
#
#     for article in recent_articles_qs:
#         if article.entities:
#             # Обработка JSON (упрощенная версия)
#             entities = article.entities
#             if isinstance(entities, str):
#                 try:
#                     entities = json.loads(entities)
#                 except json.JSONDecodeError:
#                     continue
#
#             if isinstance(entities, dict):
#                 persons = entities.get('persons', [])
#                 for person in persons[:5]:
#                     entity_counts[person] = entity_counts.get(person, 0) + 1
#                     if article.language in persons_by_lang and len(persons_by_lang[article.language]) < 10:
#                         if person not in persons_by_lang[article.language]:
#                             persons_by_lang[article.language].append(person)
#
#     top_entities = sorted(entity_counts.keys(),
#                           key=lambda x: entity_counts[x],
#                           reverse=True)[:10]
#
#     # Recent articles by language
#     languages = ['zh_cn', 'zh_tw', 'en', 'ru']
#     recent_articles = {}
#
#     for lang in languages:
#         articles = Article.objects.filter(
#             language=lang
#         ).order_by('-scraped_at')[:9]
#
#         recent_articles[lang] = [
#             ArticleSchema(
#                 id=str(art.id),
#                 news_provider=art.news_provider,
#                 title_translated=art.title_translated or art.title_origin,
#                 title_origin=art.title_origin,
#                 url=art.url,
#                 sentiment=art.sentiment or 0.0,
#                 article_bias_profile=art.bias or 0.0
#             ) for art in articles
#         ]
#
#     return DashboardResponse(
#         tier=profile.tier,
#         metrics=MetricsSchema(
#             articles_24h=articles_24h,
#             avg_sentiment=avg_sentiment,
#             top_entities=top_entities,
#             top_persons_by_language=persons_by_lang
#         ),
#         recent_articles=recent_articles
#     )
#
#
# @router.get("/articles/{language}", response=ArticleListResponse, auth=JWTAuth())
# def get_articles_by_language(
#         request,
#         language: str,
#         filters: ArticleFilterSchema = Query(...)
# ):
#     """
#     Get paginated articles for a specific language.
#     """
#     valid_languages = ['zh_cn', 'zh_tw', 'en', 'ru']
#     if language not in valid_languages:
#         raise HttpError(400, f"Invalid language. Must be one of: {', '.join(valid_languages)}")
#
#     articles_qs = Article.objects.filter(
#         language=language
#     ).order_by('-scraped_at')
#
#     paginator = Paginator(articles_qs, filters.per_page)
#     page_obj = paginator.get_page(filters.page)
#
#     return ArticleListResponse(
#         articles=[
#             ArticleSchema(
#                 id=str(art.id),
#                 news_provider=art.news_provider,
#                 title_translated=art.title_translated or art.title_origin,
#                 title_origin=art.title_origin,
#                 url=art.url,
#                 sentiment=art.sentiment or 0.0,
#                 article_bias_profile=art.bias or 0.5
#             ) for art in page_obj.object_list
#         ],
#         total=paginator.count,
#         page=page_obj.number,
#         pages=paginator.num_pages
#     )
#
#
# @router.get("/limits", response=RateLimitStatusSchema, auth=JWTAuth())
# def get_rate_limit_status(request):
#     """Get user's current rate limit status."""
#     user = request.user
#     tier = get_user_tier(user)
#     today = timezone.now().date()
#
#     from metrq_site import settings
#     limit = settings.RATE_LIMITS.get(tier, 50)
#
#     try:
#         rate_limit = RateLimit.objects.get(user=user, request_date=today)
#         used = rate_limit.request_count
#     except RateLimit.DoesNotExist:
#         used = 0
#
#     remaining = float('inf') if limit == float('inf') else max(0, limit - used)
#     reset_at = datetime.combine(today + timedelta(days=1), datetime.min.time())
#     reset_at = reset_at.replace(tzinfo=timezone.utc)
#
#     return RateLimitStatusSchema(
#         tier=tier,
#         limit=int(limit) if limit != float('inf') else -1,
#         used=used,
#         remaining=int(remaining) if remaining != float('inf') else -1,
#         reset_at=reset_at
#     )
#
#
# @router.get("/providers/{provider_name}/bias", response=BiasExplanationSchema, auth=None)
# def get_provider_bias(request, provider_name: str):
#     """
#     Get bias explanation for a news provider.
#     Public endpoint - no authentication required.
#     """
#     articles = Article.objects.filter(news_provider=provider_name).exclude(
#         bias__isnull=True
#     )[:100]
#
#     if not articles:
#         raise HttpError(404, f"No data found for provider: {provider_name}")
#
#     avg_bias = sum(a.bias or 0.5 for a in articles) / len(articles)
#
#     # Generate explanation based on bias score
#     if avg_bias < 0.3:
#         explanation = f"{provider_name} shows minimal bias in coverage."
#     elif avg_bias < 0.5:
#         explanation = f"{provider_name} shows slight bias in topic selection."
#     elif avg_bias < 0.7:
#         explanation = f"{provider_name} shows moderate bias in framing."
#     else:
#         explanation = f"{provider_name} shows strong bias in coverage."
#
#     return BiasExplanationSchema(
#         provider=provider_name,
#         bias_score=round(avg_bias, 3),
#         explanation=explanation
#     )





# """Old one"""
# router = Router(tags=["Dashboard"], auth=JWTAuth())
#
#
# class ArticleSchema(Schema):
#     id: str
#     news_provider: str
#     title_translated: str
#     title_origin: str
#     url: str
#     sentiment: float
#     article_bias_profile: float
#
#
# class DashboardMetrics(Schema):
#     articles_24h: int
#     avg_sentiment: float
#     top_entities: List[str]
#     top_persons_by_language: Dict[str, List[str]]
#
#
# class DashboardResponse(Schema):
#     tier: str
#     metrics: DashboardMetrics
#     recent_articles: Dict[str, List[ArticleSchema]]
#
#
# @router.get("/", response=DashboardResponse)
# def get_dashboard(request):
#     """Get user dashboard with metrics and recent articles"""
#     user = request.user
#     profile = user.profile
#
#     # Time range for metrics
#     last_24h = datetime.now() - timedelta(hours=24)
#
#     # Metrics calculation
#     articles_24h = Article.objects.filter(
#         scraped_at__gte=last_24h
#     ).count()
#
#     avg_sentiment_qs = Article.objects.filter(
#         scraped_at__gte=last_24h,
#         sentiment__isnull=False
#     ).aggregate(avg=Avg('sentiment'))
#
#     avg_sentiment = round(avg_sentiment_qs['avg'] or 0, 3)
#
#     # Top entities extraction (from JSONField)
#     # In production, use PostgreSQL JSONB queries for better performance
#     recent_articles_qs = Article.objects.filter(
#         scraped_at__gte=last_24h,
#         entities__isnull=False
#     ).order_by('-scraped_at')[:100]
#
#     entity_counts = {}
#     persons_by_lang = {
#         'zh_cn': [],
#         'zh_tw': [],
#         'en': [],
#         'ru': []
#     }
#
#     for article in recent_articles_qs:
#         if article.entities:
#             # Assume entities format: {'persons': [], 'organizations': [], ...}
#             persons = article.entities.get('persons', [])
#             for person in persons[:5]:  # Limit per article
#                 entity_counts[person] = entity_counts.get(person, 0) + 1
#                 if article.language in persons_by_lang and len(persons_by_lang[article.language]) < 10:
#                     if person not in persons_by_lang[article.language]:
#                         persons_by_lang[article.language].append(person)
#
#         # Fill remaining slots with generic entities if needed
#         if article.entities:
#             for entity_type, entities in article.entities.items():
#                 if isinstance(entities, list):
#                     for entity in entities[:3]:
#                         entity_counts[entity] = entity_counts.get(entity, 0) + 1
#
#     top_entities = sorted(entity_counts.keys(),
#                           key=lambda x: entity_counts[x],
#                           reverse=True)[:10]
#
#     # Recent articles by language (9 per language)
#     languages = ['zh_cn', 'zh_tw', 'en', 'ru']
#     recent_articles = {}
#
#     for lang in languages:
#         articles = Article.objects.filter(
#             language=lang
#         ).select_related().order_by('-scraped_at')[:9]
#
#         recent_articles[lang] = [
#             ArticleSchema(
#                 id=str(art.id),
#                 news_provider=art.news_provider,
#                 title_translated=art.title_translated or art.title_origin,
#                 title_origin=art.title_origin,
#                 url=art.url,
#                 sentiment=art.sentiment or 0.0,
#                 article_bias_profile=art.bias or 0.0
#             ) for art in articles
#         ]
#
#     return DashboardResponse(
#         tier=profile.tier,
#         metrics=DashboardMetrics(
#             articles_24h=articles_24h,
#             avg_sentiment=avg_sentiment,
#             top_entities=top_entities,
#             top_persons_by_language=persons_by_lang
#         ),
#         recent_articles=recent_articles
#     )
