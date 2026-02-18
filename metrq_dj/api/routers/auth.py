import re
import bcrypt
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.conf import settings
from ninja import Router, Schema
from ninja_jwt.tokens import RefreshToken
from pydantic import field_validator

from accounts.models import Profile, Organization

"""Endpoints (URL routes) for registration and login."""

router = Router(tags=["Authentication"])

# In-memory brute force protection (use Redis in production)
_brute_force_cache = {}


class RegisterSchema(Schema):
    nickname: str
    password: str
    plan: str = "free"  # 'free' or 'pro', enterprise is auto-detected

    @field_validator('nickname')
    def validate_nickname(cls, v):
        # Check length
        if len(v) < 3 or len(v) > 50:
            raise ValueError('Nickname must be between 3 and 50 characters')
        # Check allowed characters: letters, numbers, ., _, -
        if not re.match(r'^[a-zA-Z0-9.@_-]+$', v):
            raise ValueError('Nickname must be alphanumeric. Acceptable delimiters: abc.abc, abc_abc, abc-abc')
        # Check reserved words
        reserved_words = ['admin', 'support', 'root', 'info', 'system', 'test']
        nickname_lower = v.lower()
        for word in reserved_words:
            if word in nickname_lower:
                raise ValueError(f'Nickname cannot contain reserved word: {word}')
        return v

    @field_validator('password')
    def validate_password(cls, v):
        # NIST compliant: minimum 12 characters
        if len(v) < 12:
            raise ValueError('Password must be at least 12 characters')
        # Check complexity
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one digit')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
            raise ValueError('Password must contain at least one special character')
        # Check against common passwords
        if v.lower() in getattr(settings, 'COMMON_PASSWORDS', []):
            raise ValueError('Password is too common, please choose a more unique password')
        return v

    @field_validator('plan')
    def validate_plan(cls, v):
        if v not in ['free', 'pro']:
            raise ValueError('Plan must be either "free" or "pro"')
        return v


class LoginSchema(Schema):
    nickname: str
    password: str


# For login - now includes refresh token
class AuthResponse(Schema):
    user_id: str
    token: str  # access token (backward compatibility)
    access: str  # access token (explicit)
    refresh: str  # refresh token
    tier: str


# For registration only
class RegisterResponse(Schema):
    user_id: str
    token: str  # access token (backward compatibility)
    access: str  # access token (explicit)
    refresh: str  # refresh token
    tier: str
    requires_payment: bool


class ValidateResponse(Schema):
    user_id: str
    tier: str
    max_reports: int
    reports_used: int
    reports_remaining: int


def _check_brute_force(ip_address: str) -> bool:
    """Check if IP is rate limited for brute force protection"""
    now = datetime.now()
    key = f"brute_force:{ip_address}"

    if key in _brute_force_cache:
        attempts, reset_time = _brute_force_cache[key]
        if now > reset_time:
            _brute_force_cache[key] = (1, now + timedelta(minutes=1))
            return True
        if attempts >= 5:
            return False
        _brute_force_cache[key] = (attempts + 1, reset_time)
    else:
        _brute_force_cache[key] = (1, now + timedelta(minutes=1))

    return True


def _get_client_ip(request):
    """Get client IP address"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0]
    return request.META.get('REMOTE_ADDR')


def _detect_enterprise_organization(nickname: str) -> Optional[Organization]:
    """
    Detect if nickname contains an enterprise tag.
    Returns Organization if found and active, None otherwise.
    """
    separators = getattr(settings, 'ENTERPRISE_SEPARATORS', ['@'])

    for separator in separators:
        if separator in nickname:
            # Extract the part after the separator (domain/tag)
            parts = nickname.split(separator)
            if len(parts) > 1:
                # Use the last part as the enterprise tag
                tag = parts[-1].lower().strip()

                if tag:
                    try:
                        org = Organization.objects.filter(
                            enterprise_tag=tag,
                            is_active=True
                        ).first()

                        if org and org.has_available_license():
                            return org
                    except Exception:
                        # Continue to next separator if lookup fails
                        continue

    return None


def _store_refresh_token(user, refresh_token_str):
    """
    Store refresh token in OutstandingToken table for blacklisting.
    This is necessary because ROTATE_REFRESH_TOKENS is False.
    """
    from ninja_jwt.tokens import OutstandingToken
    from ninja_jwt.utils import aware_utcnow

    try:
        # Parse the refresh token to get its JTI and expiration
        token = RefreshToken(refresh_token_str)
        jti = token.get('jti')
        exp = token.get('exp')

        # Convert exp (Unix timestamp) to datetime
        # from datetime import datetime, timezone
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
        # Use get_or_create to avoid duplicate errors
        OutstandingToken.objects.get_or_create(
            jti=jti,
            defaults={
                'user': user,
                'token': refresh_token_str,
                'created_at': aware_utcnow(),
                'expires_at': expires_at,
            }
        )
        # # Check if token already exists (avoid duplicate)
        # if OutstandingToken.objects.filter(jti=jti).exists():
        #     return  # Already stored, nothing to do
        #
        # # Create the outstanding token record
        # OutstandingToken.objects.create(
        #     user=user,
        #     jti=jti,
        #     token=refresh_token_str,
        #     created_at=aware_utcnow(),
        #     expires_at=expires_at,
        # )
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to store refresh token: {e}")

# def _store_refresh_token(user, refresh_token_str):
#     """
#     Store refresh token in OutstandingToken table for blacklisting.
#     This is necessary because ROTATE_REFRESH_TOKENS is False.
#     """
#     from ninja_jwt.tokens import OutstandingToken
#     from ninja_jwt.utils import aware_utcnow
#
#     try:
#         # Parse the refresh token to get its JTI and expiration
#         token = RefreshToken(refresh_token_str)
#         jti = token.get('jti')
#         exp = token.get('exp')
#
#         # Convert exp (Unix timestamp) to datetime
#         from datetime import datetime, timezone
#         expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
#
#         # Create the outstanding token record
#         OutstandingToken.objects.create(
#             user=user,
#             jti=jti,
#             token=refresh_token_str,
#             created_at=aware_utcnow(),
#             expires_at=expires_at,
#         )
#     except Exception as e:
#         import logging
#         logger = logging.getLogger(__name__)
#         logger.warning(f"Failed to store refresh token: {e}")


def _blacklist_refresh_token(refresh_token_str):
    """
    Blacklist a refresh token by adding it to BlacklistedToken.
    Tries multiple matching strategies.
    """
    from ninja_jwt.tokens import OutstandingToken, BlacklistedToken
    import logging

    logger = logging.getLogger(__name__)

    if not refresh_token_str:
        return False

    try:
        # Strategy 1: Exact match on token string
        token = OutstandingToken.objects.get(token=refresh_token_str)
        BlacklistedToken.objects.get_or_create(token=token)
        logger.info(f"Blacklisted by exact match: {token.jti}")
        return True
    except OutstandingToken.DoesNotExist:
        pass

    try:
        # Strategy 2: Match by JTI (parse token to get JTI)
        parsed = RefreshToken(refresh_token_str)
        jti = parsed.get('jti')

        if jti:
            token = OutstandingToken.objects.get(jti=jti)
            BlacklistedToken.objects.get_or_create(token=token)
            logger.info(f"Blacklisted by JTI match: {jti}")
            return True
    except OutstandingToken.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"Error parsing token for JTI: {e}")

    # Log failure for debugging
    logger.warning(f"Token not found for blacklisting: {refresh_token_str[:50]}...")
    return False


@router.post("/register", response={201: RegisterResponse, 400: dict, 429: dict})
def register(request, data: RegisterSchema):
    """Register new user with tier-based profile and enterprise detection"""
    ip = _get_client_ip(request)

    if not _check_brute_force(ip):
        return 429, {"error": "Too many attempts. Try again later."}

    # Check if user exists
    if User.objects.filter(username=data.nickname).exists():
        return 400, {"error": "Username already exists"}

    try:
        # Detect enterprise organization
        enterprise_org = _detect_enterprise_organization(data.nickname)

        # Determine tier and payment requirement
        requires_payment = False
        final_tier = 'free'

        if enterprise_org:
            # Enterprise user: immediate enterprise status, no payment needed
            final_tier = 'enterprise'
            requires_payment = False
        elif data.plan == 'pro':
            # Pro plan selected but no enterprise: register as free, require payment
            final_tier = 'free'
            requires_payment = True
        else:
            # Free plan: immediate free status
            final_tier = 'free'
            requires_payment = False

        # Create user within transaction for atomicity
        with transaction.atomic():
            # Create user
            user = User.objects.create_user(
                username=data.nickname,
                password=data.password,  # Django handles hashing
                email=''  # Not required per spec
            )

            # Update profile
            profile = user.profile
            profile.tier = final_tier

            if enterprise_org:
                profile.organization = enterprise_org
                profile.max_reports = 999  # Enterprise unlimited
            else:
                profile.max_reports = settings.TIER_REPORT_LIMITS.get(final_tier, 1)

            profile.save()

        # Generate JWT with custom claims
        refresh = RefreshToken.for_user(user)
        refresh.payload['tier'] = final_tier

        # Store refresh token for later blacklisting
        refresh_str = str(refresh)
        _store_refresh_token(user, refresh_str)

        return 201, {
            "user_id": str(user.id),
            "token": str(refresh.access_token),  # backward compatibility
            "access": str(refresh.access_token),  # explicit access token
            "refresh": refresh_str,  # refresh token
            "tier": final_tier,
            "requires_payment": requires_payment
        }

    except Exception as e:
        return 400, {"error": str(e)}


@router.post("/login", response={200: AuthResponse, 400: dict, 429: dict})
def login(request, data: LoginSchema):
    """Login user and return JWT with refresh token"""
    ip = _get_client_ip(request)

    if not _check_brute_force(ip):
        return 429, {"error": "Too many attempts. Try again later."}

    try:
        user = User.objects.get(username=data.nickname)

        # Use Django's built-in check (handles hashing)
        if not user.check_password(data.password):
            return 400, {"error": "Invalid credentials"}

        if not user.is_active:
            return 400, {"error": "User account is disabled"}

        # Generate JWT with custom claims
        refresh = RefreshToken.for_user(user)
        refresh.payload['tier'] = user.profile.tier

        # Store refresh token for later blacklisting
        refresh_str = str(refresh)
        _store_refresh_token(user, refresh_str)

        return 200, {
            "user_id": str(user.id),
            "token": str(refresh.access_token),  # backward compatibility
            "access": str(refresh.access_token),  # explicit access token
            "refresh": refresh_str,  # refresh token
            "tier": user.profile.tier
        }

    except User.DoesNotExist:
        # Timing-safe comparison to prevent user enumeration
        bcrypt.hashpw(b'dummy', bcrypt.gensalt())
        return 400, {"error": "Invalid credentials"}


@router.post("/logout", auth=None, response={200: dict})
def logout(request):
    """
    Logout user by blacklisting the refresh token.
    Expects JSON: {"refresh": "..."} (optional).
    Always returns 200 {"success": True} even if token is invalid/missing.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        body = json.loads(request.body) if request.body else {}
        refresh_token = body.get('refresh')
    except json.JSONDecodeError:
        refresh_token = None

    # # DEBUG: Log what we received
    # if refresh_token:
    #     logger.info(f"Logout called with refresh_token: {refresh_token[:50]}...")
    # else:
    #     logger.info("Logout called with no refresh token")
    #
    # # DEBUG: Log what outstanding tokens exist
    # from ninja_jwt.tokens import OutstandingToken
    # logger.info(f"Outstanding tokens in DB: {OutstandingToken.objects.count()}")
    # for ot in OutstandingToken.objects.all()[:3]:
    #     logger.info(f"  DB Token: {ot.token[:50]}... (jti={ot.jti})")

    blacklisted = False
    if refresh_token:
        blacklisted = _blacklist_refresh_token(refresh_token)
        logger.info(f"Blacklist result: {blacklisted}")

    return 200, {"success": True, "blacklisted": blacklisted}


@router.get("/validate", auth=None, response={200: ValidateResponse, 401: dict})
def validate_token(request):
    """Validate JWT and return user details"""
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Bearer '):
        return 401, {"error": "No token provided"}

    try:
        from ninja_jwt.tokens import AccessToken
        token_str = auth_header.split(' ')[1]
        token = AccessToken(token_str)

        user_id = token.get('user_id')
        user = User.objects.select_related('profile').get(id=user_id)

        remaining = user.profile.reports_remaining
        if remaining == float('inf'):
            remaining = -1  # Indicate unlimited

        return 200, {
            "user_id": str(user.id),
            "tier": user.profile.tier,
            "max_reports": user.profile.max_reports if user.profile.max_reports < 999 else -1,
            "reports_used": user.profile.reports_used,
            "reports_remaining": remaining
        }

    except Exception:
        return 401, {"error": "Invalid token"}


# import re
# import bcrypt
# import json
# from datetime import datetime, timedelta
# from typing import Optional
# from django.contrib.auth.models import User
# from django.core.exceptions import ValidationError
# from django.db import transaction
# from django.conf import settings
# from ninja import Router, Schema
# from ninja_jwt.tokens import RefreshToken
# from pydantic import field_validator
#
# from accounts.models import Profile, Organization
#
# """Endpoints (URL routes) for registration and login."""
#
# router = Router(tags=["Authentication"])
#
# # In-memory brute force protection (use Redis in production)
# _brute_force_cache = {}
#
#
# class RegisterSchema(Schema):
#     nickname: str
#     password: str
#     plan: str = "free"  # 'free' or 'pro', enterprise is auto-detected
#
#     @field_validator('nickname')
#     def validate_nickname(cls, v):
#         # Check length
#         if len(v) < 3 or len(v) > 50:
#             raise ValueError('Nickname must be between 3 and 50 characters')
#         # Check allowed characters: letters, numbers, ., _, -
#         if not re.match(r'^[a-zA-Z0-9.@_-]+$', v):
#             raise ValueError('Nickname must be alphanumeric. Acceptable delimiters: abc.abc, abc_abc, abc-abc')
#         # Check reserved words
#         reserved_words = ['admin', 'support', 'root', 'info', 'system', 'test']
#         nickname_lower = v.lower()
#         for word in reserved_words:
#             if word in nickname_lower:
#                 raise ValueError(f'Nickname cannot contain reserved word: {word}')
#         return v
#
#
#     @field_validator('password')
#     def validate_password(cls, v):
#         # NIST compliant: minimum 12 characters
#         if len(v) < 12:
#             raise ValueError('Password must be at least 12 characters')
#         # Check complexity
#         if not re.search(r'[A-Z]', v):
#             raise ValueError('Password must contain at least one uppercase letter')
#         if not re.search(r'[a-z]', v):
#             raise ValueError('Password must contain at least one lowercase letter')
#         if not re.search(r'\d', v):
#             raise ValueError('Password must contain at least one digit')
#         if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
#             raise ValueError('Password must contain at least one special character')
#         # Check against common passwords
#         if v.lower() in getattr(settings, 'COMMON_PASSWORDS', []):
#             raise ValueError('Password is too common, please choose a more unique password')
#         return v
#
#     @field_validator('plan')
#     def validate_plan(cls, v):
#         if v not in ['free', 'pro']:
#             raise ValueError('Plan must be either "free" or "pro"')
#         return v
#
#
# class LoginSchema(Schema):
#     nickname: str
#     password: str
#
#
# # For login - now includes refresh token
# class AuthResponse(Schema):
#     user_id: str
#     token: str       # access token (backward compatibility)
#     access: str      # access token (explicit)
#     refresh: str     # refresh token
#     tier: str
#
#
# # For registration only
# class RegisterResponse(Schema):
#     user_id: str
#     token: str       # access token (backward compatibility)
#     access: str      # access token (explicit)
#     refresh: str     # refresh token
#     tier: str
#     requires_payment: bool
#
#
# class ValidateResponse(Schema):
#     user_id: str
#     tier: str
#     max_reports: int
#     reports_used: int
#     reports_remaining: int
#
#
# def _check_brute_force(ip_address: str) -> bool:
#     """Check if IP is rate limited for brute force protection"""
#     now = datetime.now()
#     key = f"brute_force:{ip_address}"
#
#     if key in _brute_force_cache:
#         attempts, reset_time = _brute_force_cache[key]
#         if now > reset_time:
#             _brute_force_cache[key] = (1, now + timedelta(minutes=1))
#             return True
#         if attempts >= 5:
#             return False
#         _brute_force_cache[key] = (attempts + 1, reset_time)
#     else:
#         _brute_force_cache[key] = (1, now + timedelta(minutes=1))
#
#     return True
#
#
# def _get_client_ip(request):
#     """Get client IP address"""
#     x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
#     if x_forwarded_for:
#         return x_forwarded_for.split(',')[0]
#     return request.META.get('REMOTE_ADDR')
#
#
# def _detect_enterprise_organization(nickname: str) -> Optional[Organization]:
#     """
#     Detect if nickname contains an enterprise tag.
#     Returns Organization if found and active, None otherwise.
#     """
#     separators = getattr(settings, 'ENTERPRISE_SEPARATORS', ['@'])
#
#     for separator in separators:
#         if separator in nickname:
#             # Extract the part after the separator (domain/tag)
#             parts = nickname.split(separator)
#             if len(parts) > 1:
#                 # Use the last part as the enterprise tag
#                 tag = parts[-1].lower().strip()
#
#                 if tag:
#                     try:
#                         org = Organization.objects.filter(
#                             enterprise_tag=tag,
#                             is_active=True
#                         ).first()
#
#                         if org and org.has_available_license():
#                             return org
#                     except Exception:
#                         # Continue to next separator if lookup fails
#                         continue
#
#     return None
#
#
# @router.post("/register", response={201: RegisterResponse, 400: dict, 429: dict})
# def register(request, data: RegisterSchema):
#     """Register new user with tier-based profile and enterprise detection"""
#     ip = _get_client_ip(request)
#
#     if not _check_brute_force(ip):
#         return 429, {"error": "Too many attempts. Try again later."}
#
#     # Check if user exists
#     if User.objects.filter(username=data.nickname).exists():
#         return 400, {"error": "Username already exists"}
#
#     try:
#         # Detect enterprise organization
#         enterprise_org = _detect_enterprise_organization(data.nickname)
#
#         # Determine tier and payment requirement
#         requires_payment = False
#         final_tier = 'free'
#
#         if enterprise_org:
#             # Enterprise user: immediate enterprise status, no payment needed
#             final_tier = 'enterprise'
#             requires_payment = False
#         elif data.plan == 'pro':
#             # Pro plan selected but no enterprise: register as free, require payment
#             final_tier = 'free'
#             requires_payment = True
#         else:
#             # Free plan: immediate free status
#             final_tier = 'free'
#             requires_payment = False
#
#         # Create user within transaction for atomicity
#         with transaction.atomic():
#             # Create user
#             user = User.objects.create_user(
#                 username=data.nickname,
#                 password=data.password,  # Django handles hashing
#                 email=''  # Not required per spec
#             )
#
#             # Update profile
#             profile = user.profile
#             profile.tier = final_tier
#
#             if enterprise_org:
#                 profile.organization = enterprise_org
#                 profile.max_reports = 999  # Enterprise unlimited
#             else:
#                 profile.max_reports = settings.TIER_REPORT_LIMITS.get(final_tier, 1)
#
#             profile.save()
#
#         # Generate JWT with custom claims
#         refresh = RefreshToken.for_user(user)
#         refresh.payload['tier'] = final_tier
#
#         return 201, {
#             "user_id": str(user.id),
#             "token": str(refresh.access_token),   # backward compatibility
#             "access": str(refresh.access_token),  # explicit access token
#             "refresh": str(refresh),              # refresh token
#             "tier": final_tier,
#             "requires_payment": requires_payment
#         }
#
#     except Exception as e:
#         return 400, {"error": str(e)}
#
#
# @router.post("/login", response={200: AuthResponse, 400: dict, 429: dict})
# def login(request, data: LoginSchema):
#     """Login user and return JWT with refresh token"""
#     ip = _get_client_ip(request)
#
#     if not _check_brute_force(ip):
#         return 429, {"error": "Too many attempts. Try again later."}
#
#     try:
#         user = User.objects.get(username=data.nickname)
#
#         # Use Django's built-in check (handles hashing)
#         if not user.check_password(data.password):
#             return 400, {"error": "Invalid credentials"}
#
#         if not user.is_active:
#             return 400, {"error": "User account is disabled"}
#
#         # Generate JWT with custom claims
#         refresh = RefreshToken.for_user(user)
#         refresh.payload['tier'] = user.profile.tier
#
#         return 200, {
#             "user_id": str(user.id),
#             "token": str(refresh.access_token),   # backward compatibility
#             "access": str(refresh.access_token),  # explicit access token
#             "refresh": str(refresh),              # refresh token
#             "tier": user.profile.tier
#         }
#
#     except User.DoesNotExist:
#         # Timing-safe comparison to prevent user enumeration
#         bcrypt.hashpw(b'dummy', bcrypt.gensalt())
#         return 400, {"error": "Invalid credentials"}


# @router.post("/logout", auth=None, response={200: dict})
# def logout(request):
#     """
#     Logout user by blacklisting the refresh token.
#     Expects JSON: {"refresh": "..."} (optional).
#     Always returns 200 {"success": True} even if token is invalid/missing.
#     """
#     from ninja_jwt.tokens import OutstandingToken, BlacklistedToken
#     # Import from token_blacklist (the app label, not the module path)
#     # from token_blacklist.models import OutstandingToken, BlacklistedToken
#
#     try:
#         body = json.loads(request.body) if request.body else {}
#         refresh_token = body.get('refresh')
#     except json.JSONDecodeError:
#         refresh_token = None
#
#     if refresh_token:
#         try:
#             # Find the outstanding token and blacklist it
#             token = OutstandingToken.objects.get(token=refresh_token)
#             BlacklistedToken.objects.get_or_create(token=token)
#         except OutstandingToken.DoesNotExist:
#             # Token already invalid or not found – ignore
#             pass
#         except Exception as e:
#             # Log unexpected errors but don't fail the logout
#             import logging
#             logger = logging.getLogger(__name__)
#             logger.warning(f"Token blacklist error: {e}")
#
#     return 200, {"success": True}


# @router.post("/logout", auth=None, response={200: dict})
# def logout(request):
#     """
#     Logout user by blacklisting the refresh token.
#     """
#     import logging
#     logger = logging.getLogger(__name__)
#
#     try:
#         body = json.loads(request.body) if request.body else {}
#         refresh_token = body.get('refresh')
#     except json.JSONDecodeError:
#         refresh_token = None
#
#     # DEBUG: Log what we received
#     logger.info(
#         f"Logout called with refresh_token: {refresh_token[:50]}..." if refresh_token else "No refresh token provided")
#
#     # DEBUG: Log what outstanding tokens exist
#     from ninja_jwt.tokens import OutstandingToken
#     logger.info(f"Outstanding tokens in DB: {OutstandingToken.objects.count()}")
#     for ot in OutstandingToken.objects.all()[:3]:
#         logger.info(f"  DB Token: {ot.token[:50]}... (jti={ot.jti})")
#
#     blacklisted = False
#     if refresh_token:
#         blacklisted = _blacklist_refresh_token(refresh_token)
#         logger.info(f"Blacklist result: {blacklisted}")
#
#     return 200, {"success": True, "blacklisted": blacklisted}




#
# @router.get("/validate", auth=None, response={200: ValidateResponse, 401: dict})
# def validate_token(request):
#     """Validate JWT and return user details"""
#     auth_header = request.headers.get('Authorization', '')
#
#     if not auth_header.startswith('Bearer '):
#         return 401, {"error": "No token provided"}
#
#     try:
#         from ninja_jwt.tokens import AccessToken
#         token_str = auth_header.split(' ')[1]
#         token = AccessToken(token_str)
#
#         user_id = token.get('user_id')
#         user = User.objects.select_related('profile').get(id=user_id)
#
#         remaining = user.profile.reports_remaining
#         if remaining == float('inf'):
#             remaining = -1  # Indicate unlimited
#
#         return 200, {
#             "user_id": str(user.id),
#             "tier": user.profile.tier,
#             "max_reports": user.profile.max_reports if user.profile.max_reports < 999 else -1,
#             "reports_used": user.profile.reports_used,
#             "reports_remaining": remaining
#         }
#
#     except Exception:
#         return 401, {"error": "Invalid token"}






# import re
# import bcrypt
# from datetime import datetime, timedelta
# from typing import Optional
# from django.contrib.auth.models import User
# from django.core.exceptions import ValidationError
# from django.db import transaction
# from django.conf import settings
# from ninja import Router, Schema
# from ninja_jwt.tokens import RefreshToken
# from pydantic import field_validator
#
# from accounts.models import Profile, Organization
#
# """Endpoints (URL routes) for registration and login."""
#
# router = Router(tags=["Authentication"])
#
# # In-memory brute force protection (use Redis in production)
# _brute_force_cache = {}
#
#
# class RegisterSchema(Schema):
#     nickname: str
#     password: str
#     plan: str = "free"  # 'free' or 'pro', enterprise is auto-detected
#
#     @field_validator('nickname')
#     def validate_nickname(cls, v):
#         # Check length
#         if len(v) < 3 or len(v) > 50:
#             raise ValueError('Nickname must be between 3 and 50 characters')
#         # Check allowed characters: letters, numbers, ., _, -
#         if not re.match(r'^[a-zA-Z0-9.@_-]+$', v):
#             raise ValueError('Nickname must be alphanumeric. Acceptable delimiters: abc.abc, abc_abc, abc-abc')
#         # Check reserved words
#         reserved_words = ['admin', 'support', 'root', 'info', 'system', 'test']
#         nickname_lower = v.lower()
#         for word in reserved_words:
#             if word in nickname_lower:
#                 raise ValueError(f'Nickname cannot contain reserved word: {word}')
#         return v
#
#
#     @field_validator('password')
#     def validate_password(cls, v):
#         # NIST compliant: minimum 12 characters
#         if len(v) < 12:
#             raise ValueError('Password must be at least 12 characters')
#         # Check complexity
#         if not re.search(r'[A-Z]', v):
#             raise ValueError('Password must contain at least one uppercase letter')
#         if not re.search(r'[a-z]', v):
#             raise ValueError('Password must contain at least one lowercase letter')
#         if not re.search(r'\d', v):
#             raise ValueError('Password must contain at least one digit')
#         if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/`~]', v):
#             raise ValueError('Password must contain at least one special character')
#         # Check against common passwords
#         if v.lower() in getattr(settings, 'COMMON_PASSWORDS', []):
#             raise ValueError('Password is too common, please choose a more unique password')
#         # Check password doesn't match nickname (case-insensitive)
#         nickname = info.data.get('nickname', '').lower()
#         if nickname and nickname in v.lower():
#             raise ValueError('Password cannot contain your nickname')
#         return v
#
#     @field_validator('plan')
#     def validate_plan(cls, v):
#         if v not in ['free', 'pro']:
#             raise ValueError('Plan must be either "free" or "pro"')
#         return v
#
#
# class LoginSchema(Schema):
#     nickname: str
#     password: str
#
# # For login
# class AuthResponse(Schema):
#     user_id: str
#     token: str
#     tier: str
#
# # For registration only
# class RegisterResponse(Schema):
#     user_id: str
#     token: str
#     tier: str
#     requires_payment: bool
#
# class ValidateResponse(Schema):
#     user_id: str
#     tier: str
#     max_reports: int
#     reports_used: int
#     reports_remaining: int
#
#
# def _check_brute_force(ip_address: str) -> bool:
#     """Check if IP is rate limited for brute force protection"""
#     now = datetime.now()
#     key = f"brute_force:{ip_address}"
#
#     if key in _brute_force_cache:
#         attempts, reset_time = _brute_force_cache[key]
#         if now > reset_time:
#             _brute_force_cache[key] = (1, now + timedelta(minutes=1))
#             return True
#         if attempts >= 5:
#             return False
#         _brute_force_cache[key] = (attempts + 1, reset_time)
#     else:
#         _brute_force_cache[key] = (1, now + timedelta(minutes=1))
#
#     return True
#
#
# def _get_client_ip(request):
#     """Get client IP address"""
#     x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
#     if x_forwarded_for:
#         return x_forwarded_for.split(',')[0]
#     return request.META.get('REMOTE_ADDR')
#
#
# def _detect_enterprise_organization(nickname: str) -> Optional[Organization]:
#     """
#     Detect if nickname contains an enterprise tag.
#     Returns Organization if found and active, None otherwise.
#     """
#     separators = getattr(settings, 'ENTERPRISE_SEPARATORS', ['@'])
#
#     for separator in separators:
#         if separator in nickname:
#             # Extract the part after the separator (domain/tag)
#             parts = nickname.split(separator)
#             if len(parts) > 1:
#                 # Use the last part as the enterprise tag
#                 tag = parts[-1].lower().strip()
#
#                 if tag:
#                     try:
#                         org = Organization.objects.filter(
#                             enterprise_tag=tag,
#                             is_active=True
#                         ).first()
#
#                         if org and org.has_available_license():
#                             return org
#                     except Exception:
#                         # Continue to next separator if lookup fails
#                         continue
#
#     return None
#
#
# def _hash_password(password: str) -> str:
#     """Hash password with bcrypt"""
#     salt = bcrypt.gensalt(rounds=12)
#     return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
#
#
# def _verify_password(password: str, hashed: str) -> bool:
#     """Verify password with timing-safe comparison"""
#     return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
#
#
# @router.post("/register", response={201: RegisterResponse, 400: dict, 429: dict})
# def register(request, data: RegisterSchema):
#     """Register new user with tier-based profile and enterprise detection"""
#     ip = _get_client_ip(request)
#
#     if not _check_brute_force(ip):
#         return 429, {"error": "Too many attempts. Try again later."}
#
#     # Check if user exists
#     if User.objects.filter(username=data.nickname).exists():
#         return 400, {"error": "Username already exists"}
#
#     try:
#         # Detect enterprise organization
#         enterprise_org = _detect_enterprise_organization(data.nickname)
#
#         # Determine tier and payment requirement
#         requires_payment = False
#         final_tier = 'free'
#
#         if enterprise_org:
#             # Enterprise user: immediate enterprise status, no payment needed
#             final_tier = 'enterprise'
#             requires_payment = False
#         elif data.plan == 'pro':
#             # Pro plan selected but no enterprise: register as free, require payment
#             final_tier = 'free'
#             requires_payment = True
#         else:
#             # Free plan: immediate free status
#             final_tier = 'free'
#             requires_payment = False
#
#         # Create user within transaction for atomicity
#         with transaction.atomic():
#             # Create user
#             user = User.objects.create_user(
#                 username=data.nickname,
#                 password=data.password,  # Django handles hashing
#                 email=''  # Not required per spec
#             )
#
#             # Update profile
#             profile = user.profile
#             profile.tier = final_tier
#
#             if enterprise_org:
#                 profile.organization = enterprise_org
#                 profile.max_reports = 999  # Enterprise unlimited
#             else:
#                 profile.max_reports = settings.TIER_REPORT_LIMITS.get(final_tier, 1)
#
#             profile.save()
#
#         # Generate JWT with custom claims
#         refresh = RefreshToken.for_user(user)
#         refresh.payload['tier'] = final_tier
#
#         return 201, {
#             "user_id": str(user.id),
#             "token": str(refresh.access_token),
#             "tier": final_tier,
#             "requires_payment": requires_payment
#         }
#
#     except Exception as e:
#         return 400, {"error": str(e)}
#
# # @router.post("/register", response={201: RegisterResponse, 400: dict, 429: dict})
# # def register(request, data: RegisterSchema):
# #     """Register new user with tier-based profile"""
# #     # TODO: rewrite register
# #     ip = _get_client_ip(request)
# #
# #     if not _check_brute_force(ip):
# #         return 429, {"error": "Too many attempts. Try again later."}
# #
# #     # Check if user exists
# #     if User.objects.filter(username=data.nickname).exists():
# #         return 400, {"error": "Username already exists"}
# #
# #     try:
# #         # Create user
# #         user = User.objects.create_user(
# #             username=data.nickname,
# #             password=data.password,  # Django handles hashing
# #             email=''  # Not required per spec
# #         )
# #
# #         # Update profile tier (created by signal)
# #         profile = user.profile  # Problem: [‘profile’ marked as unresolved attribute reference for class 'User' in PyCharm IDE]
# #         profile.tier = data.tier
# #         if data.tier == 'free':
# #             profile.max_reports = 1
# #         elif data.tier == 'pro':
# #             profile.max_reports = 10
# #         elif data.tier == 'enterprise':
# #             profile.max_reports = 999
# #         profile.save()
# #
# #         # Generate JWT
# #         refresh = RefreshToken.for_user(user)
# #
# #         return 201, {
# #             "user_id": str(user.id),
# #             # Problem: [‘id’ marked as unresolved attribute reference for class 'User' in PyCharm IDE]
# #             "token": str(refresh.access_token),
# #             # Problem: [‘access_token’ marked as unresolved attribute reference for class 'Token' in PyCharm IDE]
# #             "tier": profile.tier,
# #             "requires_payment": requires_payment
# #         }
# #
# #     except Exception as e:
# #         return 400, {"error": str(e)}
#
#
# @router.post("/login", response={200: AuthResponse, 400: dict, 429: dict})
# def login(request, data: LoginSchema):
#     """Login user and return JWT"""
#     ip = _get_client_ip(request)
#
#     if not _check_brute_force(ip):
#         return 429, {"error": "Too many attempts. Try again later."}
#
#     try:
#         user = User.objects.get(username=data.nickname)
#
#         # Use Django's built-in check (handles hashing)
#         if not user.check_password(data.password):
#             return 400, {"error": "Invalid credentials"}
#
#         if not user.is_active:
#             return 400, {"error": "User account is disabled"}
#
#         # Generate JWT with custom claims
#         refresh = RefreshToken.for_user(user)
#         refresh.payload['tier'] = user.profile.tier
#
#         return 200, {
#             "user_id": str(user.id),
#             "token": str(refresh.access_token),
#             "tier": user.profile.tier
#         }
#
#     except User.DoesNotExist:
#         # Timing-safe comparison to prevent user enumeration
#         bcrypt.hashpw(b'dummy', bcrypt.gensalt())
#         return 400, {"error": "Invalid credentials"}
#
#
# @router.get("/validate", auth=None, response={200: ValidateResponse, 401: dict})
# def validate_token(request):
#     """Validate JWT and return user details"""
#     auth_header = request.headers.get('Authorization', '')
#
#     if not auth_header.startswith('Bearer '):
#         return 401, {"error": "No token provided"}
#
#     try:
#         from ninja_jwt.tokens import AccessToken
#         token_str = auth_header.split(' ')[1]
#         token = AccessToken(token_str)
#
#         user_id = token.get('user_id')
#         user = User.objects.select_related('profile').get(id=user_id)
#
#         remaining = user.profile.reports_remaining
#         if remaining == float('inf'):
#             remaining = -1  # Indicate unlimited
#
#         return 200, {
#             "user_id": str(user.id),
#             "tier": user.profile.tier,
#             "max_reports": user.profile.max_reports if user.profile.max_reports < 999 else -1,
#             "reports_used": user.profile.reports_used,
#             "reports_remaining": remaining
#         }
#
#     except Exception:
#         return 401, {"error": "Invalid token"}
