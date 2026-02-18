import logging
import time
from functools import wraps
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from django.http import JsonResponse
from django.conf import settings
from django.db import transaction, connection
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import OperationalError, IntegrityError
from django.core.exceptions import ObjectDoesNotExist

# from django.core.cache import cache  # Use with redis

logger = logging.getLogger(__name__)

User = get_user_model()

# Добавьте в начало файла
from collections import defaultdict
from threading import Lock


class RateLimitMetrics:
    def __init__(self):
        self.lock_stats = defaultdict(int)
        self._lock = Lock()

    def increment_lock(self, lock_type='database'):
        with self._lock:
            self.lock_stats[lock_type] += 1

    def get_stats(self):
        with self._lock:
            return dict(self.lock_stats)


rate_limit_metrics = RateLimitMetrics()


def retry_on_db_lock(max_retries=5, base_delay=0.05):
    """Декоратор для повторных попыток при блокировке БД"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (OperationalError, IntegrityError) as e:
                    error_str = str(e).lower()
                    if ('database is locked' in error_str or
                        'database table is locked' in error_str or
                        'unique constraint' in error_str) and attempt < max_retries - 1:

                        # Добавляем метрику для database is locked
                        if 'database is locked' in error_str:
                            rate_limit_metrics.increment_lock('database_locked')
                        elif 'database table is locked' in error_str:
                            rate_limit_metrics.increment_lock('table_locked')
                        elif 'unique constraint' in error_str:
                            rate_limit_metrics.increment_lock('unique_violation')

                        delay = base_delay * (2 ** attempt) + (base_delay * 0.1 * (attempt + 1))
                        logger.info(f"DB contention, retry {attempt + 1}/{max_retries} after {delay:.3f}s: {e}")
                        time.sleep(delay)
                    else:
                        logger.error(f"DB operation failed after {attempt + 1} attempts: {e}")
                        raise
            return None

        return wrapper

    return decorator


class RateLimitMiddleware:
    """Tier-based rate limiting middleware with atomic database operations"""

    EXCLUDED_PATHS = getattr(settings, 'RATE_LIMIT_EXCLUDED_PATHS', [
        '/api/auth/',
        '/api/health',
        '/api/provider/',
    ])

    RATE_LIMITS = getattr(settings, 'RATE_LIMITS', {
        'free': 50,
        'pro': 5000,
        'enterprise': float('inf'),
    })

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Skip excluded paths
        for excluded in self.EXCLUDED_PATHS:
            if path.startswith(excluded):
                return self.get_response(request)

        # Get user from request
        user = self._get_user_from_request(request)
        if not user:
            return self.get_response(request)

        # Get tier from profile
        try:
            tier = user.profile.tier
        except Exception as e:
            logger.error(f"Tier in profile not found. Error: {e}. Tier set as 'Free'")
            tier = 'free'

        limit = self.RATE_LIMITS.get(tier, 50)

        # Enterprise users bypass rate limiting
        if tier == 'enterprise':
            response = self.get_response(request)
            response['X-RateLimit-Limit'] = 'unlimited'
            response['X-RateLimit-Remaining'] = 'unlimited'
            response['X-RateLimit-Reset'] = str(int(self._get_reset_time().timestamp()))
            return response

        # Atomic rate limit check and increment
        is_allowed, remaining, reset_time = self._check_and_increment(user, tier, limit)

        if not is_allowed:
            return JsonResponse({
                'error': 'Rate limit exceeded',
                'tier': tier,
                'limit': limit,
                'retry_after': int((reset_time - timezone.now()).total_seconds()),
                'upgrade_url': 'https://metrq.onrender.com/upgrade'
            }, status=429)

        # Process request
        response = self.get_response(request)

        # Add rate limit headers
        response['X-RateLimit-Limit'] = str(limit)
        response['X-RateLimit-Remaining'] = str(remaining)
        response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))
        response['Access-Control-Allow-Origin'] = 'https://metrq.onrender.com'

        return response

    @retry_on_db_lock(max_retries=3, base_delay=0.05)
    def _get_user_from_request(self, request):
        """Extract user from JWT token using AccessToken directly"""
        from ninja_jwt.tokens import AccessToken

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return None

        try:
            token_str = auth_header.split(' ')[1]
            token = AccessToken(token_str)
            user_id = token.get('user_id')
            user = User.objects.select_related('profile').get(id=user_id)
            return user
        except Exception as e:
            logger.debug(f"Auth error in rate limit: {e}")
            return None

    def _get_reset_time(self):
        """Get UTC midnight reset time as a timezone-aware datetime"""
        # Get tomorrow's date
        tomorrow = timezone.now().date() + timedelta(days=1)
        # Create UTC midnight datetime directly (without make_aware)
        return datetime.combine(tomorrow, datetime.min.time(), tzinfo=datetime_timezone.utc)

    @retry_on_db_lock(max_retries=10, base_delay=0.05)
    def _check_and_increment(self, user, tier, limit):
        """
        Универсальная версия, работающая на всех БД.
        Django сам выберет правильный уровень изоляции.
        """
        from core.models import RateLimit
        from django.db.models import F
        # Import OperationalError specifically to handle it correctly
        from django.db import OperationalError

        today = timezone.now().date()
        reset_time = self._get_reset_time()

        if tier == 'enterprise':
            return True, float('inf'), reset_time

        try:
            # 1. Get or Create the record.
            # We use a transaction to ensure get_or_create is safe,
            # but we do NOT use select_for_update to avoid locking the DB file.
            with transaction.atomic():
                rate_limit, created = RateLimit.objects.get_or_create(
                    user=user,
                    request_date=today,
                    defaults={'request_count': 0}
                )
            # 2. Atomic Increment with Limit Check
            # This performs the check "count < limit" and the increment "count + 1"
            # in a single SQL query. This is very fast and minimizes lock time.
            updated = RateLimit.objects.filter(
                user=user,
                request_date=today,
                request_count__lt=limit
            ).update(request_count=F('request_count') + 1)

            if updated:
                # If update returned 1, the increment was successful.
                # We must refresh the object to get the new value from the DB.
                rate_limit.refresh_from_db()
                remaining = limit - rate_limit.request_count
                return True, max(0, remaining), reset_time
            else:
                # If update returned 0, the condition (request_count__lt=limit) failed.
                # This means the limit was reached.
                rate_limit.refresh_from_db()
                return False, 0, reset_time

        except OperationalError:
            # CRITICAL: Re-raise OperationalError so the @retry_on_db_lock
            # decorator catches it and tries again.
            raise

        except Exception as e:
            # Catch other unexpected errors (e.g., programming errors)
            logger.error(f"Rate limit check failed unexpectedly: {e}")
            # Fail open (allow request) only for non-database-lock errors
            return True, limit - 1, reset_time

    # TODO: replace with redis driven method
    # def _check_and_increment(self, user, tier, limit):
    #     """
    #     Использует Redis для rate limiting (идеально для production).
    #     """
    #     today = timezone.now().date()
    #     reset_time = self._get_reset_time()
    #
    #     # Ключ для кэша
    #     cache_key = f"rate_limit:{user.id}:{today}"
    #
    #     try:
    #         # Атомарный инкремент в Redis
    #         current = cache.incr(cache_key)
    #
    #         # Если ключа не было, incr вернёт 1, но нужно установить TTL
    #         if current == 1:
    #             # Устанавливаем время жизни до конца дня
    #             seconds_until_midnight = (reset_time - timezone.now()).total_seconds()
    #             cache.expire(cache_key, int(seconds_until_midnight))
    #
    #         if current > limit:
    #             return False, 0, reset_time
    #
    #         remaining = limit - current
    #         return True, max(0, remaining), reset_time
    #
    #     except ValueError:
    #         # Ключ не существует, создаём
    #         cache.set(cache_key, 1, timeout=86400)  # 24 часа
    #         return True, limit - 1, reset_time
    #     except Exception as e:
    #         logger.error(f"Redis rate limit failed: {e}")
    #         # Fallback на БД
    #         return self._db_fallback_check(user, today, limit, reset_time)
    #
