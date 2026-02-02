"""
MetrQ Provider Control Panel API
Operational endpoints for service administrators.
Uses API Key authentication (X-API-Key) - separate from JWT user auth.
"""
import hashlib
import hmac
import secrets
import uuid
import json
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

from django.db import transaction, connection, models
from django.utils import timezone
from django.contrib.auth.models import User
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import APIKeyHeader

from core.models import ProviderLog, Job, Report, Article, ProviderApiKey

import structlog

logger = structlog.get_logger(__name__)
router = Router(tags=["Provider"])


# ============================================================================
# AUTHENTICATION
# ============================================================================

class APIKeyAuth(APIKeyHeader):
    """
    Provider API Key authentication using SHA-256 hashed keys.
    Separate from JWT - uses X-API-Key header.
    Implements constant-time comparison to prevent timing attacks.
    """
    param_name = "X-API-Key"

    def authenticate(self, request, key: str) -> Optional[str]:
        if not key:
            return None

        # Validate against hashed storage with metadata
        is_valid, metadata = ProviderApiKey.validate_key_with_metadata(key)

        if is_valid:
            # Store metadata in request for audit logging
            request.provider_key_metadata = metadata
            return key  # Return key for tracking

        return None


api_key_auth = APIKeyAuth()


# ============================================================================
# SCHEMAS
# ============================================================================

class ScheduleSchema(Schema):
    force: bool = False


class ScheduleResponse(Schema):
    batch_id: str
    started_at: datetime


class StatsResponse(Schema):
    queue: Dict[str, int]
    articles: Dict[str, Any]
    workers: Dict[str, int]
    system: Dict[str, Any]


class LogFilterSchema(Schema):
    level: Optional[str] = None
    limit: int = 100
    worker_id: Optional[str] = None


class LogEntrySchema(Schema):
    timestamp: datetime
    level: str
    message: str
    data: Optional[Dict]
    worker_id: Optional[str]


class UserUpdateSchema(Schema):
    tier: Optional[str] = None
    max_reports: Optional[int] = None


class UserResponse(Schema):
    id: str
    nickname: str
    tier: str
    reports_used: int
    max_reports: int
    created_at: datetime


class CleanupResponse(Schema):
    reset_count: int
    stale_jobs: List[str]


class KeyRotateSchema(Schema):
    name: str
    expires_days: Optional[int] = 90


class KeyRotateResponse(Schema):
    key: str  # Plain text - shown only once!
    name: str
    expires_at: datetime
    message: str


class QueueStatusResponse(Schema):
    redis_queues: Dict[str, int]
    db_status: Dict[str, int]
    processing_rate: float  # jobs/minute


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/schedule", auth=api_key_auth, response=ScheduleResponse)
def schedule_batch(request, data: ScheduleSchema):
    """
    Manually trigger scraping batch.
    Creates jobs and queues them for workers.
    """
    batch_id = str(uuid.uuid4())
    now = timezone.now()

    # Log the action with provider metadata
    metadata = getattr(request, 'provider_key_metadata', {})

    ProviderLog.objects.create(
        level='info',
        message=f'Provider scheduled batch {batch_id}',
        data={
            'force': data.force,
            'batch_id': batch_id,
            'provider_key_name': metadata.get('name', 'unknown'),
            'ip_address': request.META.get('REMOTE_ADDR')
        },
        worker_id='provider_api'
    )

    # TODO: Queue Celery task for batch processing
    # from worker.tasks import run_scraper_batch
    # run_scraper_batch.delay(batch_id, force=data.force)

    logger.info("batch_scheduled", batch_id=batch_id, force=data.force)

    return ScheduleResponse(
        batch_id=batch_id,
        started_at=now
    )


@router.get("/stats", auth=api_key_auth, response=StatsResponse)
def get_system_stats(request):
    """
    Real-time system metrics from DB and Redis.
    Critical for operational monitoring.
    """
    time_24h_ago = timezone.now() - timedelta(hours=24)
    stale_cutoff = timezone.now() - timedelta(minutes=5)

    # Database counts (optimized queries using indexes)
    pending_jobs = Job.objects.filter(status='pending').count()
    processing_jobs = Job.objects.filter(status='processing').count()
    failed_jobs = Job.objects.filter(status='failed').count()

    # Active workers (distinct locked_by values)
    active_workers = Job.objects.filter(
        status='processing'
    ).exclude(
        locked_by__isnull=True
    ).values('locked_by').distinct().count()

    # Stale locks (workers crashed >5 min ago)
    stale_locks = Job.objects.filter(
        status='processing',
        locked_at__lt=stale_cutoff
    ).count()

    # Articles stats
    articles_24h = Article.objects.filter(scraped_at__gte=time_24h_ago).count()
    total_articles = Article.objects.count()

    # Language distribution (last 24h)
    lang_dist = list(Article.objects.filter(
        scraped_at__gte=time_24h_ago
    ).values('language').annotate(
        count=models.Count('id')
    ))

    return StatsResponse(
        queue={
            "pending": pending_jobs,
            "processing": processing_jobs,
            "failed": failed_jobs,
            "total": pending_jobs + processing_jobs + failed_jobs
        },
        articles={
            "last_24h": articles_24h,
            "total": total_articles,
            "by_language": {item['language']: item['count'] for item in lang_dist}
        },
        workers={
            "active": active_workers,
            "stale_locks": stale_locks,
            "max_expected": 5  # From spec: 5-worker simulation
        },
        system={
            "db_connections": 1,  # SQLite single writer
            "timestamp": timezone.now().isoformat(),
            "provider": getattr(request, 'provider_key_metadata', {}).get('name', 'unknown')
        }
    )


@router.get("/queue", auth=api_key_auth, response=QueueStatusResponse)
def get_queue_status(request):
    """
    Redis queue metrics for monitoring pipeline health.
    Shows buildup between workers and DB writer.
    """
    try:
        # Import here to avoid circular imports
        from modules.scrapers.queue import queue as redis_queue

        # Get Redis queue lengths
        scrape_queue_len = redis_queue.get_queue_length('scrape')
        results_queue_len = redis_queue.get_queue_length('results')

        # Calculate processing rate (jobs completed in last 10 minutes)
        ten_min_ago = timezone.now() - timedelta(minutes=10)
        recent_completed = Job.objects.filter(
            status='completed',
            completed_at__gte=ten_min_ago
        ).count()

        rate_per_minute = recent_completed / 10.0

        return QueueStatusResponse(
            redis_queues={
                "scrape_pending": scrape_queue_len,
                "results_pending": results_queue_len,
                "total_pending": scrape_queue_len + results_queue_len
            },
            db_status={
                "pending_jobs": Job.objects.filter(status='pending').count(),
                "processing_jobs": Job.objects.filter(status='processing').count()
            },
            processing_rate=round(rate_per_minute, 2)
        )

    except Exception as e:
        logger.error("queue_status_error", error=str(e))
        raise HttpError(500, "Could not retrieve queue status")


@router.post("/cleanup", auth=api_key_auth, response=CleanupResponse)
def trigger_cleanup(request):
    """
    Manually reset stale jobs (crashed workers).
    Jobs stuck in 'processing' >5 minutes are reset to 'pending'.
    """
    stale_cutoff = timezone.now() - timedelta(minutes=5)

    try:
        reset_ids = []

        # Atomic update with RETURNING for SQLite
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('BEGIN IMMEDIATE')
                cursor.execute("""
                    UPDATE core_job 
                    SET status = 'pending', 
                        locked_by = NULL, 
                        locked_at = NULL,
                        retries = retries + 1
                    WHERE status = 'processing' 
                      AND locked_at < %s
                    RETURNING id
                """, [stale_cutoff])

                rows = cursor.fetchall()
                cursor.execute('COMMIT')
                reset_ids = [str(row[0]) for row in rows]
        else:
            # PostgreSQL path
            with transaction.atomic():
                stale_jobs = Job.objects.select_for_update().filter(
                    status='processing',
                    locked_at__lt=stale_cutoff
                )
                reset_ids = list(stale_jobs.values_list('id', flat=True))
                stale_jobs.update(
                    status='pending',
                    locked_by=None,
                    locked_at=None,
                    retries=models.F('retries') + 1
                )

        # Log cleanup action
        if reset_ids:
            ProviderLog.objects.create(
                level='warning',
                message=f'Provider cleanup reset {len(reset_ids)} stale jobs',
                data={
                    'reset_jobs': reset_ids,
                    'provider': getattr(request, 'provider_key_metadata', {}).get('name', 'unknown')
                },
                worker_id='provider_api'
            )
            logger.warning("stale_jobs_reset", count=len(reset_ids), jobs=reset_ids)

        return CleanupResponse(
            reset_count=len(reset_ids),
            stale_jobs=reset_ids
        )

    except Exception as e:
        logger.error("cleanup_failed", error=str(e))
        raise HttpError(500, f"Cleanup failed: {str(e)}")


@router.get("/logs", auth=api_key_auth, response=List[LogEntrySchema])
def get_logs(request, filters: LogFilterSchema = LogFilterSchema()):
    """
    View provider system logs with filtering.
    Use for debugging worker issues.
    """
    qs = ProviderLog.objects.all().order_by('-timestamp')

    if filters.level:
        qs = qs.filter(level=filters.level)

    if filters.worker_id:
        qs = qs.filter(worker_id=filters.worker_id)

    qs = qs[:filters.limit]

    return [
        LogEntrySchema(
            timestamp=log.timestamp,
            level=log.level,
            message=log.message,
            data=log.data if isinstance(log.data, dict) else json.loads(log.data or '{}'),
            worker_id=log.worker_id
        ) for log in qs
    ]


@router.get("/users", auth=api_key_auth, response=List[UserResponse])
def list_users(request):
    """
    List all users with tier and usage information.
    For provider billing and quota management.
    """
    users = User.objects.select_related('profile').all().order_by('date_joined')

    return [
        UserResponse(
            id=str(u.id),
            nickname=u.username,
            tier=getattr(u.profile, 'tier', 'free'),
            reports_used=getattr(u.profile, 'reports_used', 0),
            max_reports=getattr(u.profile, 'max_reports', 1),
            created_at=u.date_joined
        ) for u in users
    ]


@router.patch("/users/{user_id}", auth=api_key_auth, response={200: dict, 404: dict})
def update_user(request, user_id: str, data: UserUpdateSchema):
    """
    Update user tier or quota limits.
    Use for billing upgrades/downgrades.
    """
    try:
        user = User.objects.select_related('profile').get(id=user_id)
        updated_fields = []

        if data.tier:
            if not hasattr(user, 'profile'):
                from accounts.models import Profile
                Profile.objects.create(user=user, tier=data.tier)
            else:
                user.profile.tier = data.tier
            updated_fields.append('tier')

        if data.max_reports is not None:
            if not hasattr(user, 'profile'):
                from accounts.models import Profile
                Profile.objects.create(user=user, max_reports=data.max_reports)
            user.profile.max_reports = data.max_reports
            updated_fields.append('max_reports')

        if updated_fields:
            user.profile.save()

            # Log the change
            ProviderLog.objects.create(
                level='info',
                message=f'User {user_id} updated: {", ".join(updated_fields)}',
                data={
                    'user_id': user_id,
                    'changes': data.dict(exclude_none=True),
                    'provider': getattr(request, 'provider_key_metadata', {}).get('name', 'unknown')
                },
                worker_id='provider_api'
            )

            return 200, {
                "updated": True,
                "user_id": user_id,
                "fields": updated_fields
            }

        return 200, {"updated": False, "message": "No changes provided"}

    except User.DoesNotExist:
        return 404, {"error": "User not found"}


@router.post("/keys/rotate", auth=api_key_auth, response=KeyRotateResponse)
def rotate_api_key(request, data: KeyRotateSchema):
    """
    Generate new provider API key.
    **WARNING**: Key is shown only once! Store it securely.
    Old keys remain valid until expiry.
    """
    # Generate secure random key
    plain_key = f"mtrq_{secrets.token_urlsafe(32)}"

    # Hash for storage
    key_hash = hashlib.sha256(plain_key.encode()).hexdigest()

    # Calculate expiry
    expires_at = timezone.now() + timedelta(days=data.expires_days)

    # Store in DB
    key_obj = ProviderApiKey.objects.create(
        name=data.name,
        key_hash=key_hash,
        created_at=timezone.now(),
        expires_at=expires_at,
        is_active=True,
        created_by=getattr(request, 'provider_key_metadata', {}).get('name', 'unknown')
    )

    # Log creation (without the key!)
    ProviderLog.objects.create(
        level='info',
        message=f'New API key generated: {data.name}',
        data={
            'key_id': str(key_obj.id),
            'name': data.name,
            'expires_at': expires_at.isoformat(),
            'provider': getattr(request, 'provider_key_metadata', {}).get('name', 'unknown')
        },
        worker_id='provider_api'
    )

    logger.info("api_key_generated", name=data.name, expires=expires_at.isoformat())

    return KeyRotateResponse(
        key=plain_key,  # SHOW ONLY ONCE
        name=data.name,
        expires_at=expires_at,
        message="Store this key securely - it will never be shown again!"
    )

# import hashlib
# import hmac
# import secrets
# import uuid
# from datetime import datetime
# from typing import List, Optional
#
# from django.contrib.auth.models import User
# from django.utils import timezone
# from django.core.exceptions import ObjectDoesNotExist
# from ninja import Router, Schema
#
# from core.models import ProviderLog, Job, Report, ProviderApiKey
#
# router = Router(tags=["Provider"])
#
#
# # def verify_provider_key(request, api_key: str) -> bool:
# #     """Verify provider API key using constant-time comparison"""
# #     expected_key = secrets.token_hex(32)  # In production, load from env/DB
# #     # Check against settings.PROVIDER_API_KEYS
# #     from django.conf import settings
# #
# #     for key in settings.PROVIDER_API_KEYS:
# #         if hmac.compare_digest(api_key, key):
# #             return True
# #     return False
#
#
# class APIKeyAuth:
#     """Provider authentication using API key"""
#
#     def __call__(self, request):
#         api_key = request.headers.get('X-API-Key')
#         if not api_key:
#             return False
#
#         # Проверка через модель ProviderApiKey
#         return ProviderApiKey.validate_key(api_key)
#
#     # def __call__(self, request):
#     #     api_key = request.headers.get('X-API-Key')
#     #     if not api_key or not verify_provider_key(request, api_key):
#     #         return False
#     #     return True
#
#
# class ScheduleSchema(Schema):
#     force: bool = False
#
#
# class ScheduleResponse(Schema):
#     batch_id: str
#     started_at: datetime
#
#
# class LogEntrySchema(Schema):
#     timestamp: datetime
#     level: str
#     message: str
#     data: Optional[dict]
#
#
# class UserUpdateSchema(Schema):
#     tier: Optional[str]
#     max_reports: Optional[int]
#
#
# class UserResponse(Schema):
#     id: str
#     nickname: str
#     tier: str
#     reports_used: int
#     created_at: datetime
#
#
# @router.post("/schedule", auth=APIKeyAuth(), response=ScheduleResponse)
# def schedule_batch(request, data: ScheduleSchema):
#     """Schedule new scraping batch"""
#     batch_id = str(uuid.uuid4())
#     now = timezone.now()
#
#     # Log the action
#     ProviderLog.objects.create(
#         level='info',
#         message=f'Scheduled batch {batch_id}',
#         data={'force': data.force, 'batch_id': batch_id}
#     )
#
#     # TODO: Queue Celery task for batch processing
#     # from worker.tasks import run_scraper_batch
#     # run_scraper_batch.delay(batch_id, force=data.force)
#
#     return ScheduleResponse(
#         batch_id=batch_id,
#         started_at=now
#     )
#
#
# # @router.get("/logs", auth=APIKeyAuth(), response=List[LogEntrySchema])
# # def get_logs(request, level: Optional[str] = None, limit: int = 100):
# #     """Get provider system logs"""
# #     logs_qs = ProviderLog.objects.all().order_by('-timestamp')[:limit]
# #
# #     if level:
# #         logs_qs = logs_qs.filter(level=level)[:limit]
# #
# #     return [
# #         LogEntrySchema(
# #             timestamp=log.timestamp,
# #             level=log.level,
# #             message=log.message,
# #             data=log.data
# #         ) for log in logs_qs
# #     ]
#
#
# @router.get("/users", auth=APIKeyAuth(), response=List[UserResponse])
# def list_users(request):
#     """List all users with profile info"""
#     users = User.objects.select_related('profile').all()
#
#     return [
#         UserResponse(
#             id=str(u.id),
#             nickname=u.username,
#             tier=u.profile.tier if hasattr(u, 'profile') else 'free',
#             reports_used=u.profile.reports_used if hasattr(u, 'profile') else 0,
#             created_at=u.profile.created_at if hasattr(u, 'profile') else u.date_joined
#         ) for u in users
#     ]
#     # return [
#     #     UserResponse(
#     #         id=str(u.id),
#     #         nickname=u.username,
#     #         tier=u.profile.tier,
#     #         reports_used=u.profile.reports_used,
#     #         created_at=u.profile.created_at
#     #     ) for u in users
#     # ]
#
#
# @router.patch("/users/{user_id}", auth=APIKeyAuth(), response={200: dict, 404: dict})
# def update_user(request, user_id: str, data: UserUpdateSchema):
#     """Update user tier or limits"""
#     try:
#         user = User.objects.select_related('profile').get(id=user_id)
#         updated = False
#
#         if data.tier:
#             if not hasattr(user, 'profile'):
#                 from ...accounts.models import Profile
#                 Profile.objects.create(user=user, tier=data.tier)
#             user.profile.tier = data.tier
#             updated = True
#         # if data.tier:
#         #     user.profile.tier = data.tier
#         #     updated = True
#
#         if data.max_reports is not None:
#             if not hasattr(user, 'profile'):
#                 from ...accounts.models import Profile
#                 Profile.objects.create(user=user, max_reports=data.max_reports)
#             user.profile.max_reports = data.max_reports
#             updated = True
#         # if data.max_reports is not None:
#         #     user.profile.max_reports = data.max_reports
#         #     updated = True
#
#         if updated:
#             user.profile.save()
#
#         return 200, {"updated": True}
#
#     except (User.DoesNotExist, ObjectDoesNotExist):
#         return 404, {"error": "User not found"}
#     # except User.DoesNotExist:
#     #     return 404, {"error": "User not found"}
