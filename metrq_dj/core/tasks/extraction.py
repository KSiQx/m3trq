"""
MetrQ Layer A-E Extraction Celery Tasks
Task 14: Extract geopolitical intelligence from articles using LLM.
"""
import os
import sys
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import requests
from celery import shared_task
from django.db import transaction
from django.conf import settings

# Add project root to path for imports
worker_dir = Path(__file__).resolve().parent.parent.parent
project_root = worker_dir.parent
metrq_dj_dir = project_root / 'metrq_dj'
sys.path.insert(0, str(metrq_dj_dir))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'metriq_site.settings')

import django

django.setup()

from core.models import Article, ProviderLog
from core.models_extraction import (
    ArticleEvent, ArticleLocation, ArticleActor,
    ArticleRelationship, ArticleClaim
)
from core.llm_prompts import get_extraction_prompt, get_error_recovery_prompt
from core.choices import (
    EVENT_TYPE_CHOICES, ACTOR_TYPE_CHOICES, LOCATION_ROLE_CHOICES,
    STRATEGIC_SIGNIFICANCE_CHOICES, CONFLICT_ROLE_CHOICES,
    RELATIONSHIP_TYPE_CHOICES, CONFIDENCE_CHOICES, DIRECTION_CHOICES,
    INTENSITY_CHOICES, VERIFICATION_STATUS_CHOICES, CONFLICT_INTENSITY_CHOICES
)

logger = logging.getLogger(__name__)

# Ollama configuration
OLLAMA_HOST = getattr(settings, 'OLLAMA_HOST', 'http://localhost:11434')
OLLAMA_MODEL = getattr(settings, 'EXTRACTION_LLM_MODEL', 'qwen2.5-coder:1.5b')
OLLAMA_TIMEOUT = getattr(settings, 'EXTRACTION_LLM_TIMEOUT', 120)

# Valid choice values for validation
VALID_EVENT_TYPES = [choice[0] for choice in EVENT_TYPE_CHOICES]
VALID_ACTOR_TYPES = [choice[0] for choice in ACTOR_TYPE_CHOICES]
VALID_LOCATION_ROLES = [choice[0] for choice in LOCATION_ROLE_CHOICES]
VALID_STRATEGIC_SIGNIFICANCE = [choice[0] for choice in STRATEGIC_SIGNIFICANCE_CHOICES]
VALID_CONFLICT_ROLES = [choice[0] for choice in CONFLICT_ROLE_CHOICES]
VALID_RELATIONSHIP_TYPES = [choice[0] for choice in RELATIONSHIP_TYPE_CHOICES]
VALID_CONFIDENCE = [choice[0] for choice in CONFIDENCE_CHOICES]
VALID_DIRECTIONS = [choice[0] for choice in DIRECTION_CHOICES]
VALID_INTENSITIES = [choice[0] for choice in INTENSITY_CHOICES]
VALID_VERIFICATION_STATUS = [choice[0] for choice in VERIFICATION_STATUS_CHOICES]
VALID_CONFLICT_INTENSITY = [choice[0] for choice in CONFLICT_INTENSITY_CHOICES]


def _validate_choice(value: str, valid_choices: List[str], default: str = None) -> str:
    """Validate and normalize choice value."""
    if not value:
        return default
    value = value.lower().replace(' ', '_')
    if value in valid_choices:
        return value
    return default


def _clamp_float(value: Any, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Clamp float value to range."""
    try:
        f = float(value) if value is not None else 0.0
        return max(min_val, min(max_val, f))
    except (TypeError, ValueError):
        return 0.0


def _safe_get(data: Dict, key: str, default: Any = None) -> Any:
    """Safely get value from dict."""
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def call_ollama_llm(prompt: str, max_retries: int = 3) -> Optional[Dict]:
    """
    Call Ollama LLM with retry logic.

    Args:
        prompt: The prompt to send
        max_retries: Maximum number of retry attempts

    Returns:
        Parsed JSON response or None if failed
    """
    url = f"{OLLAMA_HOST}/api/generate"

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,  # Low temperature for deterministic output
            "num_ctx": 4096,
            "num_predict": 2048  # Limit output length
        }
    }

    for attempt in range(max_retries):
        try:
            logger.info(f"Calling Ollama LLM (attempt {attempt + 1}/{max_retries})")
            response = requests.post(
                url,
                json=payload,
                timeout=OLLAMA_TIMEOUT
            )
            response.raise_for_status()

            result = response.json()
            response_text = result.get('response', '')

            # Try to parse JSON from response
            try:
                # Clean up markdown code blocks if present
                cleaned = response_text.strip()
                if '```json' in cleaned:
                    cleaned = cleaned.split('```json')[1].split('```')[0]
                elif '```' in cleaned:
                    cleaned = cleaned.split('```')[1].split('```')[0]

                cleaned = cleaned.strip()
                data = json.loads(cleaned)
                logger.info("Successfully parsed LLM response")
                return data

            except json.JSONDecodeError as e:
                logger.warning(f"JSON parse error: {e}")
                logger.debug(f"Raw response: {response_text[:500]}")

                # If last attempt, try recovery prompt
                if attempt == max_retries - 1:
                    logger.error("All retries failed, returning None")
                    return None

        except requests.Timeout:
            logger.warning(f"Request timeout (attempt {attempt + 1})")
        except requests.RequestException as e:
            logger.warning(f"Request error: {e} (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")

    return None


def parse_extraction_response(data: Dict, article_id: str) -> Tuple[Dict, List[str]]:
    """
    Parse and validate LLM extraction response.

    Args:
        data: Parsed JSON response from LLM
        article_id: Article ID for logging

    Returns:
        Tuple of (parsed_data, error_messages)
    """
    errors = []

    if not data:
        errors.append("Empty response from LLM")
        return {}, errors

    # Check if layers is null (non-geopolitical article)
    layers = _safe_get(data, 'layers')
    if layers is None:
        logger.info(f"Article {article_id}: Non-geopolitical content detected")
        return {
            'layers': None,
            'metadata': _safe_get(data, 'metadata', {})
        }, errors

    # Validate layers structure
    if not isinstance(layers, dict):
        errors.append(f"Invalid layers type: {type(layers)}")
        return {}, errors

    # Extract and validate each layer
    result = {
        'event_types': [],
        'locations': [],
        'actors': [],
        'relationships': [],
        'conflict_dynamics': None,
        'metadata': {}
    }

    # Layer A: Event types
    event_types = _safe_get(layers, 'a_event_types', [])
    if isinstance(event_types, list):
        for et in event_types:
            validated = _validate_choice(et, VALID_EVENT_TYPES)
            if validated:
                result['event_types'].append(validated)

    # Layer B: Locations
    locations = _safe_get(layers, 'b_locations', [])
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, dict):
                result['locations'].append({
                    'name': str(_safe_get(loc, 'name', 'Unknown'))[:255],
                    'hierarchy': _safe_get(loc, 'hierarchy', []),
                    'coordinates': _safe_get(loc, 'coordinates'),
                    'role_in_event': _validate_choice(
                        _safe_get(loc, 'role_in_event'),
                        VALID_LOCATION_ROLES,
                        'reference'
                    ),
                    'strategic_significance': _validate_choice(
                        _safe_get(loc, 'strategic_significance'),
                        VALID_STRATEGIC_SIGNIFICANCE,
                        'none'
                    )
                })

    # Layer C: Actors
    actors = _safe_get(layers, 'c_actors', [])
    if isinstance(actors, list):
        for actor in actors:
            if isinstance(actor, dict):
                result['actors'].append({
                    'name': str(_safe_get(actor, 'name', 'Unknown'))[:255],
                    'type': _validate_choice(
                        _safe_get(actor, 'type'),
                        VALID_ACTOR_TYPES,
                        'other'
                    ),
                    'type_detail': str(_safe_get(actor, 'type_detail', ''))[:255] or None,
                    'roles': _safe_get(actor, 'roles', []),
                    'affiliations': _safe_get(actor, 'affiliations', []),
                    'beneficial_interests': _safe_get(actor, 'beneficial_interests', []),
                    'conflict_role': _validate_choice(
                        _safe_get(actor, 'conflict_role'),
                        VALID_CONFLICT_ROLES
                    ),
                    'power_index': _clamp_float(_safe_get(actor, 'power_index'))
                })

    # Layer D: Relationships
    relationships = _safe_get(layers, 'd_relationships', [])
    if isinstance(relationships, list):
        for rel in relationships:
            if isinstance(rel, dict):
                result['relationships'].append({
                    'source': str(_safe_get(rel, 'source', 'Unknown'))[:255],
                    'target': str(_safe_get(rel, 'target', 'Unknown'))[:255],
                    'relationship_type': _validate_choice(
                        _safe_get(rel, 'relationship_type'),
                        VALID_RELATIONSHIP_TYPES,
                        'other'
                    ),
                    'evidence': str(_safe_get(rel, 'evidence', ''))[:10000],
                    'confidence': _validate_choice(
                        _safe_get(rel, 'confidence'),
                        VALID_CONFIDENCE,
                        'medium'
                    ),
                    'direction': _validate_choice(
                        _safe_get(rel, 'direction'),
                        VALID_DIRECTIONS,
                        'unidirectional'
                    ),
                    'intensity': _validate_choice(
                        _safe_get(rel, 'intensity'),
                        VALID_INTENSITIES
                    )
                })

    # Layer E: Conflict dynamics
    conflict_dynamics = _safe_get(layers, 'e_conflict_dynamics')
    if isinstance(conflict_dynamics, dict):
        result['conflict_dynamics'] = conflict_dynamics

    # Metadata
    metadata = _safe_get(data, 'metadata', {})
    if isinstance(metadata, dict):
        result['metadata'] = {
            'sentiment_score': _clamp_float(_safe_get(metadata, 'sentiment_score'), -1, 1),
            'sentiment_label': _validate_choice(
                _safe_get(metadata, 'sentiment_label'),
                ['positive', 'negative', 'neutral'],
                'neutral'
            ),
            'subjectivity': _clamp_float(_safe_get(metadata, 'subjectivity')),
            'reliability': _clamp_float(_safe_get(metadata, 'reliability')),
            'corruption_risk': _clamp_float(_safe_get(metadata, 'corruption_risk')),
            'money_laundering_risk': _clamp_float(_safe_get(metadata, 'money_laundering_risk')),
            'sanctions_risk': _clamp_float(_safe_get(metadata, 'sanctions_risk')),
            'claims': _safe_get(metadata, 'claims', []),
            'processed_text_length': int(_safe_get(metadata, 'processed_text_length', 0)),
            'warning': str(_safe_get(metadata, 'warning', ''))[:500] or None,
            'conflict_intensity': _validate_choice(
                _safe_get(metadata, 'conflict_intensity'),
                VALID_CONFLICT_INTENSITY,
                'low'
            ),
            'geopolitical_relevance': _clamp_float(_safe_get(metadata, 'geopolitical_relevance'))
        }

    return result, errors


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue='llm_extraction'
)
def extract_layer_a_e(self, article_id: str):
    """
    Extract Layer A-E geopolitical intelligence from an article.

    This task:
    1. Fetches the article with 'analyzing' or 'important' status
    2. Calls Ollama LLM to extract structured intelligence
    3. Parses and validates the response
    4. Saves extracted data to database models
    5. Updates article status and metadata

    Args:
        article_id: UUID of the article to process

    Returns:
        Dict with extraction results and statistics
    """
    logger.info(f"Starting Layer A-E extraction for article {article_id}")

    try:
        # Fetch article
        try:
            article = Article.objects.get(id=article_id)
        except Article.DoesNotExist:
            logger.error(f"Article {article_id} not found")
            return {"error": "Article not found", "article_id": article_id}

        # Check if article has text to analyze
        text = article.text_origin or article.title_origin
        if not text:
            logger.error(f"Article {article_id} has no text to analyze")
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])
            return {"error": "No text content", "article_id": article_id}

        # Check status - should be 'analyzing' or 'important' (from Task 13)
        if article.status not in ['analyzing', 'important', 'new']:
            logger.warning(f"Article {article_id} has unexpected status: {article.status}")
            # Continue anyway, but log the warning

        # Set status to 'analyzing' to prevent duplicate processing
        if article.status != 'analyzing':
            article.status = 'analyzing'
            article.save(update_fields=['status', 'updated_at'])

        # Build prompt
        is_short = len(text) < 500
        prompt = get_extraction_prompt(text, is_short=is_short)

        # Call LLM
        response_data = call_ollama_llm(prompt)

        if response_data is None:
            # LLM call failed after retries
            logger.error(f"LLM extraction failed for article {article_id}")
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])

            # Log error
            ProviderLog.objects.create(
                level='error',
                message=f'Layer A-E extraction failed: LLM call failed',
                data={'article_id': str(article_id), 'error': 'LLM timeout or parse error'},
                worker_id='extraction_task'
            )

            # Retry the task
            raise self.retry(exc=Exception("LLM extraction failed"), countdown=120)

        # Parse and validate response
        parsed_data, parse_errors = parse_extraction_response(response_data, str(article_id))

        if not parsed_data:
            logger.error(f"Failed to parse LLM response for article {article_id}")
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])

            ProviderLog.objects.create(
                level='error',
                message=f'Layer A-E extraction failed: Parse error',
                data={
                    'article_id': str(article_id),
                    'errors': parse_errors,
                    'raw_response': str(response_data)[:1000]
                },
                worker_id='extraction_task'
            )

            return {
                "error": "Parse failed",
                "article_id": article_id,
                "parse_errors": parse_errors
            }

        # Save data within transaction
        with transaction.atomic():
            # Update article metadata fields
            metadata = parsed_data.get('metadata', {})

            article.sentiment = metadata.get('sentiment_score')
            article.subjectivity = metadata.get('subjectivity')
            article.reliability = metadata.get('reliability')
            article.corruption_risk = metadata.get('corruption_risk')
            article.money_laundering_risk = metadata.get('money_laundering_risk')
            article.sanctions_risk = metadata.get('sanctions_risk')
            article.geopolitical_relevance = metadata.get('geopolitical_relevance')
            article.conflict_intensity = metadata.get('conflict_intensity')
            article.conflict_dynamics = parsed_data.get('conflict_dynamics')
            article.status = 'analyzed'
            article.save()

            # Clear existing extraction data (for reprocessing)
            article.events.all().delete()
            article.locations.all().delete()
            article.actors.all().delete()
            article.relationships.all().delete()
            article.claims.all().delete()

            # Save Layer A: Event types
            for event_type in parsed_data.get('event_types', []):
                ArticleEvent.objects.create(
                    article=article,
                    event_type=event_type,
                    confidence=1.0  # Could be extracted from LLM in future
                )

            # Save Layer B: Locations
            for loc_data in parsed_data.get('locations', []):
                ArticleLocation.objects.create(
                    article=article,
                    name=loc_data['name'],
                    hierarchy=loc_data.get('hierarchy', []),
                    coordinates=loc_data.get('coordinates'),
                    role_in_event=loc_data.get('role_in_event', 'reference'),
                    strategic_significance=loc_data.get('strategic_significance', 'none')
                )

            # Save Layer C: Actors
            created_actors = {}  # Track created actors for relationship linking
            for actor_data in parsed_data.get('actors', []):
                actor_name = actor_data['name']
                actor, created = ArticleActor.objects.update_or_create(
                    article=article,
                    name=actor_name,
                    defaults={
                        'type': actor_data.get('type', 'other'),
                        'type_detail': actor_data.get('type_detail'),
                        'roles': actor_data.get('roles', []),
                        'affiliations': actor_data.get('affiliations', []),
                        'beneficial_interests': actor_data.get('beneficial_interests', []),
                        'conflict_role': actor_data.get('conflict_role'),
                        'power_index': actor_data.get('power_index')
                    }
                )
                created_actors[actor_name] = actor

            # Save Layer D: Relationships
            for rel_data in parsed_data.get('relationships', []):
                source_name = rel_data['source']
                target_name = rel_data['target']

                # Create minimal actors if they don't exist (referential integrity)
                if source_name not in created_actors:
                    actor, _ = ArticleActor.objects.get_or_create(
                        article=article,
                        name=source_name,
                        defaults={'type': 'unknown'}
                    )
                    created_actors[source_name] = actor
                    logger.warning(
                        f"Created minimal actor for relationship source: {source_name}"
                    )

                if target_name not in created_actors:
                    actor, _ = ArticleActor.objects.get_or_create(
                        article=article,
                        name=target_name,
                        defaults={'type': 'unknown'}
                    )
                    created_actors[target_name] = actor
                    logger.warning(
                        f"Created minimal actor for relationship target: {target_name}"
                    )

                ArticleRelationship.objects.create(
                    article=article,
                    source=source_name,
                    target=target_name,
                    relationship_type=rel_data.get('relationship_type', 'other'),
                    evidence=rel_data.get('evidence', ''),
                    confidence=rel_data.get('confidence', 'medium'),
                    direction=rel_data.get('direction', 'unidirectional'),
                    intensity=rel_data.get('intensity')
                )

            # Save Claims
            for claim_data in metadata.get('claims', []):
                if isinstance(claim_data, dict):
                    ArticleClaim.objects.create(
                        article=article,
                        text=str(claim_data.get('text', ''))[:2000],
                        verification_status=claim_data.get('verification_status', 'unverified'),
                        confidence=_clamp_float(claim_data.get('confidence')),
                        supporting_evidence=claim_data.get('supporting_evidence'),
                        contradicting_evidence=claim_data.get('contradicting_evidence')
                    )

        # Log success
        stats = {
            'events': len(parsed_data.get('event_types', [])),
            'locations': len(parsed_data.get('locations', [])),
            'actors': len(parsed_data.get('actors', [])),
            'relationships': len(parsed_data.get('relationships', [])),
            'claims': len(metadata.get('claims', []))
        }

        logger.info(f"Extraction complete for article {article_id}: {stats}")

        ProviderLog.objects.create(
            level='info',
            message=f'Layer A-E extraction completed successfully',
            data={
                'article_id': str(article_id),
                'stats': stats,
                'parse_errors': parse_errors
            },
            worker_id='extraction_task'
        )

        return {
            "success": True,
            "article_id": article_id,
            "status": "analyzed",
            "stats": stats,
            "parse_errors": parse_errors if parse_errors else None
        }

    except Exception as e:
        logger.exception(f"Unexpected error in extraction task for {article_id}")

        # Try to mark article as failed
        try:
            article = Article.objects.get(id=article_id)
            article.status = 'failed'
            article.save(update_fields=['status', 'updated_at'])
        except:
            pass

        # Log error
        ProviderLog.objects.create(
            level='error',
            message=f'Layer A-E extraction failed with exception',
            data={
                'article_id': str(article_id),
                'error': str(e),
                'error_type': type(e).__name__
            },
            worker_id='extraction_task'
        )

        # Retry on unexpected errors
        raise self.retry(exc=e, countdown=120)


@shared_task(queue='llm_extraction')
def batch_extract_articles(limit: int = 10):
    """
    Batch process articles awaiting extraction.

    Args:
        limit: Maximum number of articles to process in this batch

    Returns:
        Dict with batch processing results
    """
    logger.info(f"Starting batch extraction for up to {limit} articles")

    # Find articles ready for extraction
    articles = Article.objects.filter(
        status__in=['analyzing', 'important', 'new'],
        text_origin__isnull=False
    ).exclude(
        text_origin=''
    )[:limit]

    results = {
        'queued': 0,
        'failed': 0,
        'errors': []
    }

    for article in articles:
        try:
            # Queue individual extraction task
            extract_layer_a_e.delay(str(article.id))
            results['queued'] += 1
        except Exception as e:
            logger.error(f"Failed to queue extraction for {article.id}: {e}")
            results['failed'] += 1
            results['errors'].append(str(article.id))

    logger.info(f"Batch extraction queued: {results}")
    return results
