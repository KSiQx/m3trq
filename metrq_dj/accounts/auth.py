from ninja_jwt.tokens import RefreshToken
from ninja_jwt.tokens import OutstandingToken, BlacklistedToken


"""Authentication business logic (token generation, custom checks)"""


def jwt_payload_handler(user):
    """Generating a Custom Payload for a JWT Token"""
    refresh = RefreshToken.for_user(user)
    # profile = getattr(user, 'profile', None)

    # We add data to the token to avoid accessing the database for each request.
    tier = 'free'
    if hasattr(user, 'profile'):
        tier = user.profile.tier
    # tier = getattr(profile, 'tier', 'free') if profile else 'free'
    refresh.payload['tier'] = tier

    # HERE: You can add other important IDs or flags.
    # refresh.payload['is_pro'] = (tier == 'pro')

    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
        'user_id': str(user.id),
        'tier': tier,
    }

def revoke_user_tokens(user):
    """Revoke all refresh tokens for a user (useful for password change, downgrade, etc.)."""
    outstanding_tokens = OutstandingToken.objects.filter(user=user)
    for token in outstanding_tokens:
        BlacklistedToken.objects.get_or_create(token=token)
