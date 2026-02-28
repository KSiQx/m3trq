# worker/tasks/importance.py
"""
MetrQ Importance Classification Module
Multi-level filtering funnel: L2 (LLM-based) with future L1 (FastText) support
"""
import os
import sys
import asyncio
import json
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass

# Django setup for standalone execution
worker_dir = Path(__file__).resolve().parent.parent
project_root = worker_dir.parent
metrq_dj_dir = project_root / 'metrq_dj'

sys.path.insert(0, str(metrq_dj_dir))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

import django

django.setup()

from celery import shared_task
from django.db import transaction
from django.conf import settings
from django.utils import timezone

from core.models import Article, ProviderLog

import structlog

logger = structlog.get_logger()

# Global semaphore to limit parallel LLM requests within this worker
# Note: For single GPU production, set CELERY_WORKER_CONCURRENCY=1 for llm_tasks queue
_ollama_semaphore: Optional[asyncio.Semaphore] = None


def get_semaphore() -> asyncio.Semaphore:
    """Lazy initialization of semaphore to avoid event loop issues at import time."""
    global _ollama_semaphore
    if _ollama_semaphore is None:
        # Limit concurrent LLM calls per worker (protects GPU from overload)
        concurrency_limit = getattr(settings, 'IMPORTANCE_LLM_CONCURRENCY', 1)
        _ollama_semaphore = asyncio.Semaphore(concurrency_limit)
    return _ollama_semaphore


@dataclass
class ClassificationResult:
    """Result of importance classification for a single article."""
    article_id: str
    is_important: bool
    confidence: float
    error: Optional[str] = None


IMPORTANCE_PROMPT_TEMPLATE = """You are a geopolitical news analyzer. Determine whether the following news item is important for geopolitical/risk analysis.

News is considered important if it contains:
- military conflicts, combat operations, terrorist attacks;
- political crises, government resignations, sanctions;
- economic shocks (devaluation, default, trade wars);
- natural disasters with large-scale consequences;
- intelligence actions, cyberattacks;
- protests, riots.

News about sports, culture, entertainment, everyday events without a geopolitical context, and scientific discoveries without political impact are considered unimportant.

Parse the following headers (each on a separate line) and return a JSON array of objects with the fields "is_important" (bool) and "confidence" (float from 0 to 1). The response must be strictly in JSON format without any additional explanations.

Headers:
{headers}

JSON Response:"""


async def call_llm_with_semaphore(
        prompt: str,
        model: str = None,
        timeout: int = None,
        max_retries: int = 3
) -> Optional[str]:
    """
    Call Ollama LLM with semaphore-controlled concurrency and retry logic.

    Args:
        prompt: The prompt to send to LLM
        model: Model name (defaults to settings.IMPORTANCE_LLM_MODEL)
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts

    Returns:
        LLM response text or None if all retries failed
    """
    semaphore = get_semaphore()
    model = model or getattr(settings, 'IMPORTANCE_LLM_MODEL', 'qwen2.5-coder:1.5b')
    timeout = timeout or getattr(settings, 'IMPORTANCE_LLM_TIMEOUT', 30)
    ollama_host = getattr(settings, 'OLLAMA_HOST', 'http://ollama:11434')

    async with semaphore:
        for attempt in range(max_retries):
            start_time = asyncio.get_event_loop().time()
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{ollama_host}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "options": {
                                "temperature": 0.1,  # Low temperature for deterministic classification
                                "num_ctx": 2048
                            }
                        }
                    )
                    response.raise_for_status()
                    result = response.json()
                    response_text = result.get('response', '')

                    duration = asyncio.get_event_loop().time() - start_time
                    logger.debug(
                        "llm_call_success",
                        model=model,
                        duration_seconds=round(duration, 2),
                        attempt=attempt + 1,
                        response_length=len(response_text)
                    )
                    return response_text

            except httpx.TimeoutException:
                duration = asyncio.get_event_loop().time() - start_time
                logger.warning(
                    "llm_timeout",
                    model=model,
                    timeout_seconds=timeout,
                    attempt=attempt + 1,
                    duration_seconds=round(duration, 2)
                )
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                    await asyncio.sleep(wait_time)

            except httpx.HTTPError as e:
                logger.error(
                    "llm_http_error",
                    model=model,
                    error=str(e),
                    attempt=attempt + 1
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

            except Exception as e:
                logger.error(
                    "llm_unexpected_error",
                    model=model,
                    error=str(e),
                    attempt=attempt + 1
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

    # All retries exhausted
    logger.error("llm_all_retries_failed", model=model, max_retries=max_retries)
    return None


def parse_llm_response(response_text: str, expected_count: int) -> List[ClassificationResult]:
    """
    Parse JSON response from LLM into classification results.

    Args:
        response_text: Raw LLM response
        expected_count: Expected number of results

    Returns:
        List of ClassificationResult objects
    """
    if not response_text:
        return []

    # Clean up markdown code blocks if present
    cleaned = response_text.strip()
    if '```json' in cleaned:
        cleaned = cleaned.split('```json')[1].split('```')[0]
    elif '```' in cleaned:
        cleaned = cleaned.split('```')[1].split('```')[0]

    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)

        # Handle both single object and array responses
        if isinstance(data, dict):
            data = [data]

        results = []
        for item in data[:expected_count]:
            is_important = bool(item.get('is_important', False))
            confidence = float(item.get('confidence', 0.5))
            # Clamp confidence to [0, 1]
            confidence = max(0.0, min(1.0, confidence))
            results.append(ClassificationResult(
                article_id="",  # Will be filled later
                is_important=is_important,
                confidence=confidence
            ))

        # Pad with defaults if LLM returned fewer items
        while len(results) < expected_count:
            results.append(ClassificationResult(
                article_id="",
                is_important=False,
                confidence=0.0,
                error="Missing in LLM response"
            ))

        return results

    except json.JSONDecodeError as e:
        logger.error("json_parse_failed", response_text=response_text[:200], error=str(e))
        return []
    except (KeyError, TypeError, ValueError) as e:
        logger.error("response_format_error", response_text=response_text[:200], error=str(e))
        return []


def _run_async_classification(articles: List[Article]) -> List[ClassificationResult]:
    """Synchronous wrapper for async LLM classification."""
    if not articles:
        return []

    # Build prompt with article titles
    headers = []
    for article in articles:
        title = article.title_translated or article.title_origin
        headers.append(f"- {title}")

    prompt = IMPORTANCE_PROMPT_TEMPLATE.format(headers="\n".join(headers))

    # Run async call in sync context
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        response_text = loop.run_until_complete(call_llm_with_semaphore(prompt))
    finally:
        loop.close()

    if response_text is None:
        # All retries failed - mark all as failed
        return [
            ClassificationResult(
                article_id=str(art.id),
                is_important=False,
                confidence=0.0,
                error="LLM call failed after all retries"
            )
            for art in articles
        ]

    # Parse results
    results = parse_llm_response(response_text, len(articles))

    # Map results to article IDs
    for i, article in enumerate(articles):
        results[i].article_id = str(article.id)

    return results

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue='llm_tasks'
)
def classify_importance_batch(self):
    """
    Periodic Celery task to classify importance of new articles using LLM.

    Fetches articles with 'new' status, classifies via LLM in batches,
    and updates status to 'analyzing' (important) or 'skipped' (not important).
    """
    batch_size = getattr(settings, 'IMPORTANCE_BATCH_SIZE', 20)
    use_fasttext = getattr(settings, 'IMPORTANCE_USE_FASTTEXT', False)

    logger.info(
        "importance_batch_start",
        batch_size=batch_size,
        use_fasttext=use_fasttext
    )

    # Fetch articles awaiting classification
    articles = list(
        Article.objects.filter(
            status='new'
        ).order_by(
            '-scraped_at'  # Process newest first
        )[:batch_size]
    )

    if not articles:
        logger.debug("no_new_articles_to_classify")
        return {"processed": 0, "important": 0, "skipped": 0, "failed": 0}

    article_ids = [str(a.id) for a in articles]
    logger.info(
        "articles_fetched_for_classification",
        count=len(articles),
        article_ids=article_ids
    )

    # Future: Check if FastText model should be used
    if use_fasttext:
        # This will be implemented when FastText model is trained
        logger.warning("fasttext_not_implemented_yet", fallback_to_llm=True)

    # Classify using LLM
    results = _run_async_classification(articles)

    # Update articles in database
    stats = {"processed": 0, "important": 0, "skipped": 0, "failed": 0}

    with transaction.atomic():
        for article, result in zip(articles, results):
            try:
                article.importance_score = result.confidence

                if result.error:
                    # Classification failed - mark as failed
                    article.status = 'failed'
                    # Store error in entities field temporarily or use a dedicated approach
                    logger.error(
                        "classification_failed_for_article",
                        article_id=str(article.id),
                        error=result.error
                    )
                    stats["failed"] += 1

                elif result.is_important:
                    article.status = 'analyzing'
                    stats["important"] += 1
                    logger.debug(
                        "article_marked_important",
                        article_id=str(article.id),
                        confidence=result.confidence
                    )
                else:
                    article.status = 'skipped'
                    stats["skipped"] += 1
                    logger.debug(
                        "article_marked_skipped",
                        article_id=str(article.id),
                        confidence=result.confidence
                    )

                article.save(update_fields=['importance_score', 'status', 'updated_at'])
                stats["processed"] += 1

            except Exception as e:
                logger.error(
                    "article_update_failed",
                    article_id=str(article.id),
                    error=str(e)
                )
                stats["failed"] += 1

    # Log completion
    logger.info(
        "importance_batch_complete",
        **stats,
        duration_ms=None  # Could add timing if needed
    )

    # Log to ProviderLog for operational visibility
    if stats["failed"] > 0:
        ProviderLog.objects.create(
            level='warning',
            message=f'Importance classification completed with {stats["failed"]} failures',
            data={
                'stats': stats,
                'article_ids': article_ids
            },
            worker_id='importance_classifier'
        )

    return stats

@shared_task(queue='llm_tasks')
def reclassify_article(article_id: str):
    """
    Re-classify a single article (for admin/manual use).
    Useful for testing or reprocessing specific articles.
    """
    try:
        article = Article.objects.get(id=article_id)
    except Article.DoesNotExist:
        logger.error("article_not_found", article_id=article_id)
        return {"error": "Article not found"}

    # Reset to new status to force reclassification
    article.status = 'new'
    article.importance_score = None
    article.save(update_fields=['status', 'importance_score'])

    # Trigger batch processing
    result = classify_importance_batch.delay()

    return {"article_id": article_id, "task_id": result.id}

# Convenience function for manual testing
def classify_single_article_sync(article_id: str) -> Optional[ClassificationResult]:
    """
    Synchronous helper for testing classification on a single article.
    Usage: python -c "from worker.tasks.importance import classify_single_article_sync; print(classify_single_article_sync('uuid'))"
    """
    try:
        article = Article.objects.get(id=article_id)
    except Article.DoesNotExist:
        return None

    results = _run_async_classification([article])
    return results[0] if results else None
