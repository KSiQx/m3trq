import hashlib
import logging
import time

from datetime import datetime, time, timedelta, timezone as dt_timezone
from django.http import JsonResponse
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.contrib.auth import get_user_model

from core.models import RateLimit


logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Tier-based rate limiting middleware"""

    EXCLUDED_PATHS = [
        '/api/auth/',
        '/api/health',
        '/api/provider/',
    ]

    RATE_LIMITS = {
        'free': 50,
        'pro': 5000,
        'enterprise': float('inf'),
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Skip excluded paths
        for excluded in self.EXCLUDED_PATHS:
            if path.startswith(excluded):
                return self.get_response(request)

        # Get a user via ninja_jwt
        user = self._get_user_from_request(request)
        if not user:
            return self.get_response(request)

        # Get tier from profile
        try:
            tier = user.profile.tier
        except Exception:
            tier = 'free'

        # Check limits
        is_allowed, remaining, reset_time = self._check_rate_limit(user, tier)

        response = self.get_response(request)

        # Add headings
        limit = self.RATE_LIMITS.get(tier, 50)
        response['X-RateLimit-Limit'] = str(limit) if tier != 'enterprise' else 'unlimited'
        response['X-RateLimit-Remaining'] = str(remaining) if tier != 'enterprise' else 'unlimited'
        response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))

        if not is_allowed:
            return JsonResponse({
                'error': 'Rate limit exceeded',
                'tier': tier,
                'limit': limit,
                'retry_after': int((reset_time - timezone.now()).total_seconds()),
                'upgrade_url': 'https://metrq.onrender.com/upgrade'
            }, status=429)

        return response

    def _get_user_from_request(self, request):
        """Get user from request"""
        from ninja_jwt.authentication import JWTAuth

        try:
            # Using JWTAuth for authentication
            auth = JWTAuth()
            result = auth.authenticate(request)
            if result:
                return result[0]  # Returns (user, token)
        except Exception as e:
            logger.debug(f"Auth error in rate limit: {e}")

        return None

    def _check_rate_limit(self, user, tier):
        """Check and update limits"""
        today = timezone.now().date()
        limit = self.RATE_LIMITS.get(tier, 50)

        # Enterprise users without restrictions
        if tier == 'enterprise':
            return True, float('inf'), timezone.now() + timedelta(days=1)

        # Cache key
        cache_key = f"rate_limit:{user.id}:{today}"

        # Получить из кэша
        cached = cache.get(cache_key)
        if cached is not None:
            if cached >= limit:
                # Calculate the reset time (the next UTC day at midnight)
                reset_time = datetime.combine(
                    today + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=dt_timezone.utc
                )
                return False, 0, reset_time

            # Increase counter by 1
            cache.incr(cache_key)
            remaining = limit - (cached + 1)
            reset_time = datetime.combine(
                today + timedelta(days=1),
                datetime.min.time(),
                tzinfo=dt_timezone.utc
            )
            return True, remaining, reset_time

        # BD fallback
        try:
            # from core.models import RateLimit
            rate_obj, created = RateLimit.objects.get_or_create(
                user=user,
                request_date=today,
                defaults={'request_count': 1}
            )

            if not created:
                if rate_obj.request_count >= limit:
                    reset_time = datetime.combine(
                        today + timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=dt_timezone.utc
                    )
                    return False, 0, reset_time

                # Atomic augmentation
                from django.db.models import F
                RateLimit.objects.filter(
                    user=user,
                    request_date=today
                ).update(request_count=F('request_count') + 1)
                rate_obj.refresh_from_db()

            # Save in cache for 1 day
            cache.set(cache_key, rate_obj.request_count, timeout=86400)

            remaining = limit - rate_obj.request_count
            reset_time = datetime.combine(
                today + timedelta(days=1),
                datetime.min.time(),
                tzinfo=dt_timezone.utc
            )
            return True, remaining, reset_time

        except Exception as e:
            logger.error(f"Rate limit DB error: {e}")
            return True, limit, timezone.now() + timedelta(days=1)

# class RateLimitMiddleware:
#     """Tier-based rate limiting middleware"""
#
#     EXCLUDED_PATHS = [
#         '/api/auth/register',
#         '/api/auth/login',
#         '/api/auth/refresh',
#         '/api/health',
#         '/api/provider/',
#     ]
#
#     RATE_LIMITS = {
#         'free': 50,
#         'pro': 5000,
#         'enterprise': float('inf'),
#     }
#
#     def __init__(self, get_response):
#         self.get_response = get_response
#
#     def __call__(self, request):
#         # Skip rate limiting for excluded paths
#         path = request.path
#         for excluded in self.EXCLUDED_PATHS:
#             if path.startswith(excluded):
#                 return self.get_response(request)
#
#         # Only rate limit authenticated requests
#         auth_header = request.headers.get('Authorization', '')
#         if not auth_header.startswith('Bearer '):
#             return self.get_response(request)
#
#         # Extract user from JWT (simplified - in production use ninja_jwt authentication)
#         try:
#             user = self._get_user_from_token(auth_header)
#             if not user:
#                 return self.get_response(request)
#
#             tier = getattr(user, 'profile', None)
#             tier = tier.tier if tier else 'free'
#
#             # Check rate limit
#             is_allowed, remaining, reset_time = self._check_rate_limit(user, tier)
#
#             response = self.get_response(request)
#
#             # Add rate limit headers
#             limit = self.RATE_LIMITS.get(tier, 50)
#             response['X-RateLimit-Limit'] = str(limit) if tier != 'enterprise' else 'unlimited'
#             response['X-RateLimit-Remaining'] = str(remaining) if tier != 'enterprise' else 'unlimited'
#             response['X-RateLimit-Reset'] = str(int(reset_time.timestamp()))
#
#             if not is_allowed:
#                 return JsonResponse({
#                     'error': 'Rate limit exceeded',
#                     'tier': tier,
#                     'limit': limit,
#                     'retry_after': int((reset_time - datetime.utcnow()).total_seconds()),
#                     'upgrade_url': 'https://metrq.onrender.com/upgrade'
#                 }, status=429)
#
#             return response
#
#         except Exception as e:
#             logger.error(f"Rate limiting error: {e}")
#             return self.get_response(request)
#
#     def _get_user_from_token(self, auth_header):
#         """Extract user from JWT token"""
#         from ninja_jwt.tokens import AccessToken
#         from django.contrib.auth import get_user_model
#
#         User = get_user_model()
#         token = auth_header.split(' ')[1]
#
#         try:
#             access_token = AccessToken(token)
#             user_id = access_token.get('user_id')
#             return User.objects.get(id=user_id)
#         except Exception:
#             return None
#
#     def _check_rate_limit(self, user, tier):
#         """Check and update rate limit for user"""
#         today = date.today()
#         limit = self.RATE_LIMITS.get(tier, 50)
#
#         # Enterprise users have unlimited access
#         if tier == 'enterprise':
#             return True, float('inf'), datetime.utcnow() + timedelta(days=1)
#
#         # Use cache for high-performance rate limiting (fallback to DB)
#         cache_key = f"rate_limit:{user.id}:{today}"
#         cached_count = cache.get(cache_key)
#
#         if cached_count is not None:
#             if cached_count >= limit:
#                 reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
#                 return False, 0, reset_time
#
#             cache.incr(cache_key)
#             remaining = limit - (cached_count + 1)
#             reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
#             return True, remaining, reset_time
#
#         # Fallback to database
#         try:
#             rate_limit, created = RateLimit.objects.get_or_create(
#                 user=user,
#                 request_date=today,
#                 defaults={'request_count': 0}
#             )
#
#             if rate_limit.request_count >= limit:
#                 reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
#                 return False, 0, reset_time
#
#             # Atomic increment using F() expression for concurrency safety
#             from django.db.models import F
#             RateLimit.objects.filter(
#                 user=user,
#                 request_date=today
#             ).update(request_count=F('request_count') + 1)
#
#             # Refresh from DB
#             rate_limit.refresh_from_db()
#             remaining = limit - rate_limit.request_count
#
#             # Cache for 1 day
#             cache.set(cache_key, rate_limit.request_count, timeout=86400)
#
#             reset_time = datetime.combine(today + timedelta(days=1), datetime.min.time())
#             return True, remaining, reset_time
#
#         except Exception as e:
#             logger.error(f"Database rate limit check failed: {e}")
#             # Fail open in case of DB error (allow request)
#             return True, limit, datetime.utcnow() + timedelta(days=1)  # Use django.utils.timezone.now().
