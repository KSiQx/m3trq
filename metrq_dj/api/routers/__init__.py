from ninja import NinjaAPI
from ninja_jwt.controller import NinjaJWTDefaultController

api = NinjaAPI(
    title="MetrQ API",
    version="1.0.0",
    description="News Analytics SaaS API",
    urls_namespace="api"
)

# Add JWT controller
api.add_router("/auth", NinjaJWTDefaultController)

# Import and register routers
from .auth import router as auth_router
from .dashboard import router as dashboard_router
from .reports import router as reports_router
from .provider import router as provider_router
from .health import router as health_router

api.add_router("/auth", auth_router)
api.add_router("/dashboard", dashboard_router)
api.add_router("/reports", reports_router)
api.add_router("/provider", provider_router)
api.add_router("/health", health_router)
