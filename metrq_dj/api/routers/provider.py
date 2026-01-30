import hashlib
import hmac
import secrets
import uuid
from datetime import datetime
from typing import List, Optional
from django.contrib.auth.models import User
from ninja import Router, Schema
from core.models import ProviderLog, Job, Report

router = Router(tags=["Provider"])


def verify_provider_key(request, api_key: str) -> bool:
    """Verify provider API key using constant-time comparison"""
    expected_key = secrets.token_hex(32)  # In production, load from env/DB
    # Check against settings.PROVIDER_API_KEYS
    from django.conf import settings

    for key in settings.PROVIDER_API_KEYS:
        if hmac.compare_digest(api_key, key):
            return True
    return False


class APIKeyAuth:
    def __call__(self, request):
        api_key = request.headers.get('X-API-Key')
        if not api_key or not verify_provider_key(request, api_key):
            return False
        return True


class ScheduleSchema(Schema):
    force: bool = False


class ScheduleResponse(Schema):
    batch_id: str
    started_at: datetime


class LogEntrySchema(Schema):
    timestamp: datetime
    level: str
    message: str
    data: Optional[dict]


class UserUpdateSchema(Schema):
    tier: Optional[str]
    max_reports: Optional[int]


class UserResponse(Schema):
    id: str
    nickname: str
    tier: str
    reports_used: int
    created_at: datetime


@router.post("/schedule", auth=APIKeyAuth(), response=ScheduleResponse)
def schedule_batch(request, data: ScheduleSchema):
    """Schedule new scraping batch"""
    batch_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Log the action
    ProviderLog.objects.create(
        level='info',
        message=f'Scheduled batch {batch_id}',
        data={'force': data.force, 'batch_id': batch_id}
    )

    # TODO: Queue Celery task for batch processing
    # from worker.tasks import run_scraper_batch
    # run_scraper_batch.delay(batch_id, force=data.force)

    return ScheduleResponse(
        batch_id=batch_id,
        started_at=now
    )


@router.get("/logs", auth=APIKeyAuth(), response=List[LogEntrySchema])
def get_logs(request, level: Optional[str] = None, limit: int = 100):
    """Get provider system logs"""
    logs_qs = ProviderLog.objects.all().order_by('-timestamp')[:limit]

    if level:
        logs_qs = logs_qs.filter(level=level)[:limit]

    return [
        LogEntrySchema(
            timestamp=log.timestamp,
            level=log.level,
            message=log.message,
            data=log.data
        ) for log in logs_qs
    ]


@router.get("/users", auth=APIKeyAuth(), response=List[UserResponse])
def list_users(request):
    """List all users with profile info"""
    users = User.objects.select_related('profile').all()

    return [
        UserResponse(
            id=str(u.id),
            nickname=u.username,
            tier=u.profile.tier,
            reports_used=u.profile.reports_used,
            created_at=u.profile.created_at
        ) for u in users
    ]


@router.patch("/users/{user_id}", auth=APIKeyAuth(), response={200: dict, 404: dict})
def update_user(request, user_id: str, data: UserUpdateSchema):
    """Update user tier or limits"""
    try:
        user = User.objects.select_related('profile').get(id=user_id)
        updated = False

        if data.tier:
            user.profile.tier = data.tier
            updated = True

        if data.max_reports is not None:
            user.profile.max_reports = data.max_reports
            updated = True

        if updated:
            user.profile.save()

        return 200, {"updated": True}

    except User.DoesNotExist:
        return 404, {"error": "User not found"}