from datetime import datetime, timedelta
from typing import List, Dict
from django.db.models import Avg, Count, Q
from ninja import Router, Schema
from ninja.security import HttpBearer
from ninja_jwt.authentication import JWTAuth
from core.models import Article


router = Router(tags=["Dashboard"], auth=JWTAuth())


class ArticleSchema(Schema):
    id: str
    news_provider: str
    title_translated: str
    title_origin: str
    url: str
    sentiment: float
    article_bias_profile: float


class DashboardMetrics(Schema):
    articles_24h: int
    avg_sentiment: float
    top_entities: List[str]
    top_persons_by_language: Dict[str, List[str]]


class DashboardResponse(Schema):
    tier: str
    metrics: DashboardMetrics
    recent_articles: Dict[str, List[ArticleSchema]]


@router.get("/", response=DashboardResponse)
def get_dashboard(request):
    """Get user dashboard with metrics and recent articles"""
    user = request.user
    profile = user.profile

    # Time range for metrics
    last_24h = datetime.now() - timedelta(hours=24)

    # Metrics calculation
    articles_24h = Article.objects.filter(
        scraped_at__gte=last_24h
    ).count()

    avg_sentiment_qs = Article.objects.filter(
        scraped_at__gte=last_24h,
        sentiment__isnull=False
    ).aggregate(avg=Avg('sentiment'))

    avg_sentiment = round(avg_sentiment_qs['avg'] or 0, 3)

    # Top entities extraction (from JSONField)
    # In production, use PostgreSQL JSONB queries for better performance
    recent_articles_qs = Article.objects.filter(
        scraped_at__gte=last_24h,
        entities__isnull=False
    ).order_by('-scraped_at')[:100]

    entity_counts = {}
    persons_by_lang = {
        'zh_cn': [],
        'zh_tw': [],
        'en': [],
        'ru': []
    }

    for article in recent_articles_qs:
        if article.entities:
            # Assume entities format: {'persons': [], 'organizations': [], ...}
            persons = article.entities.get('persons', [])
            for person in persons[:5]:  # Limit per article
                entity_counts[person] = entity_counts.get(person, 0) + 1
                if article.language in persons_by_lang and len(persons_by_lang[article.language]) < 10:
                    if person not in persons_by_lang[article.language]:
                        persons_by_lang[article.language].append(person)

        # Fill remaining slots with generic entities if needed
        if article.entities:
            for entity_type, entities in article.entities.items():
                if isinstance(entities, list):
                    for entity in entities[:3]:
                        entity_counts[entity] = entity_counts.get(entity, 0) + 1

    top_entities = sorted(entity_counts.keys(),
                          key=lambda x: entity_counts[x],
                          reverse=True)[:10]

    # Recent articles by language (9 per language)
    languages = ['zh_cn', 'zh_tw', 'en', 'ru']
    recent_articles = {}

    for lang in languages:
        articles = Article.objects.filter(
            language=lang
        ).select_related().order_by('-scraped_at')[:9]

        recent_articles[lang] = [
            ArticleSchema(
                id=str(art.id),
                news_provider=art.news_provider,
                title_translated=art.title_translated or art.title_origin,
                title_origin=art.title_origin,
                url=art.url,
                sentiment=art.sentiment or 0.0,
                article_bias_profile=art.bias or 0.0
            ) for art in articles
        ]

    return DashboardResponse(
        tier=profile.tier,
        metrics=DashboardMetrics(
            articles_24h=articles_24h,
            avg_sentiment=avg_sentiment,
            top_entities=top_entities,
            top_persons_by_language=persons_by_lang
        ),
        recent_articles=recent_articles
    )
