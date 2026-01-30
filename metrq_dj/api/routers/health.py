from datetime import datetime
from ninja import Router, Schema
from django.db import connection
import redis

router = Router(tags=["Health"])


class HealthResponse(Schema):
    status: str
    queue_length: int
    db_connections: int
    timestamp: str


@router.get("/", auth=None, response=HealthResponse)
def health_check(request):
    """System health check endpoint"""

    # Check Redis queue (Celery)
    queue_length = 0
    try:
        from django.conf import settings
        r = redis.from_url(settings.REDIS_URL)
        # Check default Celery queue
        queue_length = r.llen('celery') or 0
    except Exception:
        queue_length = -1

    # Check DB connection
    db_connections = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            db_connections = 1  # Simplified - in production use pg_stat_activity for PG
    except Exception:
        db_connections = 0

    return HealthResponse(
        status="healthy" if db_connections > 0 else "unhealthy",
        queue_length=queue_length,
        db_connections=db_connections,
        timestamp=datetime.utcnow().isoformat()
    )