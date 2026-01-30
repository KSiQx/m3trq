import re
import bcrypt
from datetime import datetime, timedelta
from typing import Optional
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from ninja import Router, Schema
from ninja_jwt.tokens import RefreshToken
from pydantic import validator

router = Router(tags=["Authentication"])

# In-memory brute force protection (use Redis in production)
_brute_force_cache = {}


class RegisterSchema(Schema):
    nickname: str
    password: str
    tier: str = "free"

    @validator('nickname')
    def validate_nickname(cls, v):
        if len(v) < 3 or len(v) > 50:
            raise ValueError('Nickname must be between 3 and 50 characters')
        if not re.match(r'^[a-zA-Z0-9_]+$', v):
            raise ValueError('Nickname must be alphanumeric')
        return v

    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        return v

    @validator('tier')
    def validate_tier(cls, v):
        if v not in ['free', 'pro', 'enterprise']:
            raise ValueError('Invalid tier')
        return v


class LoginSchema(Schema):
    nickname: str
    password: str


class AuthResponse(Schema):
    user_id: str
    token: str
    tier: str


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


def _hash_password(password: str) -> str:
    """Hash password with bcrypt"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def _verify_password(password: str, hashed: str) -> bool:
    """Verify password with timing-safe comparison"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


@router.post("/register", response={201: AuthResponse, 400: dict, 429: dict})
def register(request, data: RegisterSchema):
    """Register new user with tier-based profile"""
    ip = _get_client_ip(request)

    if not _check_brute_force(ip):
        return 429, {"error": "Too many attempts. Try again later."}

    # Check if user exists
    if User.objects.filter(username=data.nickname).exists():
        return 400, {"error": "Username already exists"}

    try:
        # Create user
        user = User.objects.create_user(
            username=data.nickname,
            password=data.password,  # Django handles hashing
            email=''  # Not required per spec
        )

        # Update profile tier (created by signal)
        profile = user.profile
        profile.tier = data.tier
        if data.tier == 'free':
            profile.max_reports = 1
        elif data.tier == 'pro':
            profile.max_reports = 10
        elif data.tier == 'enterprise':
            profile.max_reports = 999
        profile.save()

        # Generate JWT
        refresh = RefreshToken.for_user(user)

        return 201, {
            "user_id": str(user.id),
            "token": str(refresh.access_token),
            "tier": profile.tier
        }

    except Exception as e:
        return 400, {"error": str(e)}


@router.post("/login", response={200: AuthResponse, 400: dict, 429: dict})
def login(request, data: LoginSchema):
    """Login user and return JWT"""
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

        return 200, {
            "user_id": str(user.id),
            "token": str(refresh.access_token),
            "tier": user.profile.tier
        }

    except User.DoesNotExist:
        # Timing-safe comparison to prevent user enumeration
        bcrypt.hashpw(b'dummy', bcrypt.gensalt())
        return 400, {"error": "Invalid credentials"}


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
