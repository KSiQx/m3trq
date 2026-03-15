"""
Celery tasks for article translation.
Implements multi-service translation with LLM fallback.
"""
import os
import sys
import logging
from pathlib import Path

# Django setup for Celery
worker_dir = Path(__file__).resolve().parent.parent.parent
project_root = worker_dir.parent
sys.path.insert(0, str(worker_dir))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

import django

django.setup()

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.models import Article, ProviderLog

# Import translation modules
sys.path.insert(0, str(project_root / 'modules'))
from translation import translate_to_russian, translate_with_hy_mt
# from translation.translation_service import translate_to_russian
# from translation.llm_translator import translate_with_hy_mt

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue='translation',
    time_limit=600,  # 10 minutes max
    soft_time_limit=540,  # 9 minutes soft limit
)
def translate_article(self, article_id: str):
    """
    Translate article to Russian using fallback chain.

    Flow:
    1. Online services (Google, Baidu, Yandex) with chunking
    2. Local LLM (HY-MT) if all online fail

    Status transitions:
    - analyzed -> translating (when task starts)
    - translating -> translated (success)
    - translating -> failed (all attempts exhausted)
    """
    try:
        # Fetch article
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            logger.error(f"Article {article_id} not found")
            return {"status": "error", "message": "Article not found"}

        # Idempotency check
        if article.status == 'translated' and article.text_translated:
            logger.info(f"Article {article_id} already translated, skipping")
            return {"status": "skipped", "message": "Already translated"}

        if article.status not in ('analyzed', 'translating', 'failed'):
            logger.warning(f"Article {article_id} has unexpected status: {article.status}")
            return {"status": "error", "message": f"Invalid status: {article.status}"}

        # Set status to translating
        article.status = 'translating'
        article.save(update_fields=['status', 'updated_at'])

        source_lang = article.language

        logger.info(f"Starting translation for article {article_id} ({source_lang})")

        # Track timing
        start_time = timezone.now()

        # Step 1: Try online services
        translated_text = None
        service_used = None

        # Translate title (usually short, single chunk)
        title_translated = None

        if article.title_origin:
            title_translated, title_service = translate_to_russian(
                article.title_origin,
                source_lang
            )
            if title_translated:
                service_used = title_service
                logger.info(f"Title translated with {title_service}")

        # Translate main text
        if article.text_origin:
            translated_text, text_service = translate_to_russian(
                article.text_origin,
                source_lang
            )

            if translated_text:
                service_used = text_service
                logger.info(f"Text translated with {text_service}")

        # Step 2: Fallback to HY-MT if online services failed
        if translated_text is None and article.text_origin:
            logger.warning("All online services failed, falling back to HY-MT")

            ProviderLog.objects.create(
                level='warning',
                message=f'Translation fallback to HY-MT for article {article_id}',
                data={
                    'article_id': str(article_id),
                    'source_lang': source_lang,
                    'attempted_services': 'online_chain'
                },
                worker_id='translation_worker'
            )

            translated_text = translate_with_hy_mt(article.text_origin, source_lang)
            service_used = 'HY-MT:1.8b' if translated_text else None

            if title_translated is None and article.title_origin:
                # Also translate title with HY-MT
                title_translated = translate_with_hy_mt(article.title_origin, source_lang)

        # Step 3: Update article based on result
        duration = (timezone.now() - start_time).total_seconds()

        if translated_text:
            with transaction.atomic():
                article.title_translated = title_translated or article.title_origin
                article.text_translated = translated_text
                article.translator = service_used
                article.status = 'translated'
                article.save(update_fields=[
                    'title_translated', 'text_translated',
                    'translator', 'status', 'updated_at'
                ])

            # Log success
            ProviderLog.objects.create(
                level='info',
                message=f'Translation completed for article {article_id}',
                data={
                    'article_id': str(article_id),
                    'service': service_used,
                    'duration_seconds': duration,
                    'source_lang': source_lang,
                    'text_length': len(article.text_origin or ''),
                    'translated_length': len(translated_text)
                },
                worker_id='translation_worker'
            )

            logger.info(f"Translation completed in {duration:.2f}s using {service_used}")

            return {
                "status": "success",
                "article_id": str(article_id),
                "service": service_used,
                "duration_seconds": duration
            }

        else:
            # All translation attempts failed
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])

            ProviderLog.objects.create(
                level='error',
                message=f'All translation attempts failed for article {article_id}',
                data={
                    'article_id': str(article_id),
                    'source_lang': source_lang,
                    'retries': self.request.retries
                },
                worker_id='translation_worker'
            )

            # Retry if we haven't exhausted retries
            if self.request.retries < self.max_retries:
                logger.warning(f"Retrying translation for {article_id} (attempt {self.request.retries + 1})")
                raise self.retry(countdown=120 * (self.request.retries + 1))

            logger.error(f"Translation permanently failed for article {article_id}")
            return {
                "status": "failed",
                "article_id": str(article_id),
                "message": "All translation services failed"
            }

    except Exception as e:
        logger.exception(f"Unexpected error in translate_article: {e}")

        # Try to mark article as failed
        try:
            article = Article.objects.get(id=article_id)
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])
        except:
            pass

        # Retry on unexpected errors
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60)

        raise


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    queue='llm_postprocess',  # Separate queue for GPU-intensive postprocessing
    time_limit=300,
    soft_time_limit=240,
)
def improve_translation(self, article_id: str):
    """
    Post-process translation to improve quality using LLM.

    This task takes an already-translated article and uses a larger
    LLM (qwen2.5-coder:1.5b) to polish the translation while preserving
    factual accuracy.

    Can be chained after translate_article:
    translate_article.s(article_id) | improve_translation.s()

    Status: Currently designed but not activated (future enhancement).
    """
    import requests

    OLLAMA_POSTPROCESS_HOST = os.environ.get('OLLAMA_POSTPROCESS_HOST', 'http://ollama-postprocess:11435')
    OLLAMA_POSTPROCESS_MODEL = os.environ.get('OLLAMA_POSTPROCESS_MODEL', 'qwen2.5-coder:1.5b')

    try:
        article = Article.objects.get(id=article_id)

        # Only process if already translated
        if article.status != 'translated' or not article.text_translated:
            logger.warning(f"Article {article_id} not ready for postprocessing")
            return {"status": "skipped", "message": "Article not translated yet"}

        # Prepare improvement prompt
        prompt = f"""Improve the following Russian translation while preserving all facts, names, and terms.
Make it more natural and readable, but do not add or remove information.

Original ({article.language}):
{article.text_origin[:2000]}

Current Russian translation:
{article.text_translated[:3000]}

Provide only the improved Russian translation, no explanations:"""

        response = requests.post(
            f"{OLLAMA_POSTPROCESS_HOST}/api/generate",
            json={
                "model": OLLAMA_POSTPROCESS_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_ctx": 4096,
                }
            },
            timeout=120
        )

        response.raise_for_status()
        data = response.json()

        improved_text = data.get('response', '').strip()

        if improved_text and len(improved_text) > len(article.text_translated) * 0.8:
            with transaction.atomic():
                article.text_translated = improved_text
                article.translator = f"{article.translator}+postprocess"
                article.save(update_fields=['text_translated', 'translator', 'updated_at'])

            logger.info(f"Translation improved for article {article_id}")

            ProviderLog.objects.create(
                level='info',
                message=f'Translation postprocessed for article {article_id}',
                data={'article_id': str(article_id)},
                worker_id='postprocess_worker'
            )

            return {"status": "success", "article_id": str(article_id)}
        else:
            logger.warning(f"Postprocessing produced invalid result for {article_id}")
            return {"status": "failed", "message": "Invalid postprocessing result"}

    except Article.DoesNotExist:
        logger.error(f"Article {article_id} not found")
        return {"status": "error", "message": "Article not found"}

    except Exception as e:
        logger.exception(f"Postprocessing failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60)
        raise
