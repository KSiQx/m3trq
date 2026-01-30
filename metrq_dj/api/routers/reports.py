import uuid
from datetime import datetime
from typing import Optional
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.security import HttpBearer
from ninja_jwt.authentication import JWTAuth
from core.models import Report
from django.http import HttpResponse

router = Router(tags=["Reports"], auth=JWTAuth())


class ReportRequestSchema(Schema):
    type: str  # weekly, monthly
    format: str  # pdf, excel


class ReportResponse(Schema):
    job_id: str
    status: str


class ReportStatusSchema(Schema):
    status: str
    download_url: Optional[str]


@router.post("/request", response={201: ReportResponse, 400: dict, 403: dict})
def request_report(request, data: ReportRequestSchema):
    """Request new report generation"""
    user = request.user
    profile = user.profile

    # Validate type and format
    if data.type not in ['weekly', 'monthly']:
        return 400, {"error": "Invalid report type. Use 'weekly' or 'monthly'"}
    if data.format not in ['pdf', 'excel']:
        return 400, {"error": "Invalid format. Use 'pdf' or 'excel'"}

    # Check quota
    if not profile.can_generate_report():
        return 403, {
            "error": "Report quota exceeded",
            "tier": profile.tier,
            "max_reports": profile.max_reports,
            "reports_used": profile.reports_used,
            "upgrade_url": "https://metrq.onrender.com/upgrade"
        }

    # Create report job
    report = Report.objects.create(
        user=user,
        type=data.type,
        format=data.format,
        status='queued'
    )

    # Increment used count (not yet implemented - do after completion)
    # For now, increment immediately (can be adjusted based on business logic)
    profile.reports_used += 1
    profile.save()

    # TODO: Queue Celery task for report generation
    # from worker.tasks import generate_report
    # generate_report.delay(str(report.id))

    return 201, ReportResponse(
        job_id=str(report.id),
        status='queued'
    )


@router.get("/status/{job_id}", response=ReportStatusSchema)
def get_report_status(request, job_id: str):
    """Get report generation status"""
    report = get_object_or_404(Report, id=job_id, user=request.user)

    download_url = None
    if report.status == 'done':
        download_url = f"/api/reports/download/{report.id}"

    return ReportStatusSchema(
        status=report.status,
        download_url=download_url
    )


@router.get("/download/{job_id}")
def download_report(request, job_id: str):
    """Download completed report"""
    report = get_object_or_404(Report, id=job_id, user=request.user)

    if report.status != 'done':
        return {"error": "Report not ready"}, 400

    if not report.file_blob:
        return {"error": "File not found"}, 404

    # Determine content type
    if report.format == 'pdf':
        content_type = 'application/pdf'
        extension = 'pdf'
    else:
        content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        extension = 'xlsx'

    response = HttpResponse(
        report.file_blob,
        content_type=content_type
    )
    response['Content-Disposition'] = f'attachment; filename="report_{report.id}.{extension}"'

    return response