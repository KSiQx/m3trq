"""
MetrQ Report Generation API
Async report generation with tier-based queue routing.
PDF via ReportLab + matplotlib, Excel via pandas/openpyxl.
"""

import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from io import BytesIO

from django.shortcuts import get_object_or_404
from django.http import HttpResponse, FileResponse
from django.core.cache import cache
from django.conf import settings

from ninja import Router, Schema
from ninja.security import HttpBearer
from ninja.errors import HttpError
from ninja_jwt.authentication import JWTAuth

from core.models import Report
from accounts.models import Profile

router = Router(tags=["Reports"], auth=JWTAuth())


# ============================================================================
# SCHEMAS
# ============================================================================

class ReportRequestSchema(Schema):
    type: str  # daily, weekly, monthly, quarterly, semi_annual, annual
    format: str  # pdf, excel


class ReportRequestResponse(Schema):
    job_id: str
    status: str
    estimated_time_seconds: int
    message: str


class ReportStatusSchema(Schema):
    job_id: str
    status: str  # queued, processing, done, failed
    progress: int  # 0-100
    download_url: Optional[str]
    error: Optional[str]

class ReportLimitError(Schema):
    error: str
    tier: str
    max_reports: int
    reports_used: int
    upgrade_url: str

# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/request", response={200: ReportRequestResponse, 403: ReportLimitError})
def request_report(request, data: ReportRequestSchema):
    """
    Request new report generation task.
    Routes to appropriate queue based on user tier.
    """
    user = request.user
    profile = user.profile

    # Validate type and format
    valid_types = ['daily', 'weekly', 'monthly', 'quarterly', 'semi_annual', 'annual']
    valid_formats = ['pdf', 'excel']

    if data.type not in valid_types:
        raise HttpError(400, f"Invalid type. Must be one of: {', '.join(valid_types)}")
    if data.format not in valid_formats:
        raise HttpError(400, f"Invalid format. Must be one of: {', '.join(valid_formats)}")

    # Check quota
    if not profile.can_generate_report():
        return 403, {
            "error": "Report limit reached",
            "tier": profile.tier,
            "max_reports": profile.max_reports,
            "reports_used": profile.reports_used,
            "upgrade_url": settings.UPGRADE_URL
        }

    # Create report job
    job_id = str(uuid.uuid4())
    report = Report.objects.create(
        id=job_id,
        user=user,
        type=data.type,
        format=data.format,
        status='queued'
    )

    # Increment used count
    profile.reports_used += 1
    profile.save()

    # Determine queue based on tier
    tier = profile.tier
    if tier == 'free':
        # Free users go to slow queue, enforce 60s minimum
        queue_name = 'reports_slow'
        estimated_time = 60 if data.format == 'pdf' else 30
    else:
        # Pro/enterprise get fast queue
        queue_name = 'reports_fast'
        estimated_time = 30 if data.format == 'pdf' else 15

    # Queue Celery task
    if data.format == 'pdf':
        from worker.tasks.reports import generate_pdf_report
        task = generate_pdf_report.apply_async(
            args=[job_id, str(user.id), data.type],
            queue=queue_name
        )
    else:
        from worker.tasks.reports import generate_excel_report
        task = generate_excel_report.apply_async(
            args=[job_id, str(user.id), data.type],
            queue=queue_name
        )

    # Store task ID for status tracking
    cache.set(f"metrq:report:{job_id}:task_id", task.id, timeout=3600)
    cache.set(f"metrq:report:{job_id}:progress", 0, timeout=3600)

    return ReportRequestResponse(
        job_id=job_id,
        status='queued',
        estimated_time_seconds=estimated_time,
        message=f"Report queued in {queue_name} queue. Estimated time: {estimated_time}s."
    )


@router.get("/status/{job_id}", response=ReportStatusSchema)
def get_report_status(request, job_id: str):
    """Get report generation status with progress."""
    report = get_object_or_404(Report, id=job_id, user=request.user)

    # Get progress from Redis cache
    progress = cache.get(f"metrq:report:{job_id}:progress", 0)

    download_url = None
    if report.status == 'done':
        download_url = f"/api/reports/download/{report.id}"

    return ReportStatusSchema(
        job_id=job_id,
        status=report.status,
        progress=progress,
        download_url=download_url,
        error=report.error_message
    )


@router.get("/download/{job_id}")
def download_report(request, job_id: str):
    """Download completed report."""
    report = get_object_or_404(Report, id=job_id, user=request.user)

    if report.status != 'done':
        raise HttpError(400, {"error": "Report not ready"})

    if not report.file_blob:
        raise HttpError(404, {"error": "Report file not found"})

    # Determine content type and filename
    if report.format == 'pdf':
        content_type = 'application/pdf'
        extension = 'pdf'
        filename = f"metrq_report_{report.type}_{job_id[:8]}.pdf"
    else:
        content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        extension = 'xlsx'
        filename = f"metrq_report_{report.type}_{job_id[:8]}.xlsx"

    # Create response with proper headers
    response = HttpResponse(
        report.file_blob,
        content_type=content_type
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Content-Length'] = len(report.file_blob)

    return response


@router.get("/history")
def get_report_history(request):
    """Get user's report generation history."""
    reports = Report.objects.filter(user=request.user).order_by('-created_at')[:10]

    return [
        {
            "job_id": str(r.id),
            "type": r.type,
            "format": r.format,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "download_url": f"/api/reports/download/{r.id}" if r.status == 'done' else None
        }
        for r in reports
    ]
