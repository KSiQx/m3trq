from django.urls import path
from ninja import NinjaAPI

# Initialize API first
api = NinjaAPI(
    title="MetrQ API",
    version="1.0.0",
    description="News Analytics SaaS API",
    urls_namespace="api"
)

# Import routers - must be after api initialization but before add_router
from .routers.auth import router as auth_router
from .routers.dashboard import router as dashboard_router
from .routers.reports import router as reports_router
from .routers.provider import router as provider_router
from .routers.health import router as health_router

# Add routers
api.add_router("/auth", auth_router, tags=["auth"])
api.add_router("/dashboard", dashboard_router, tags=["dashboard"])
api.add_router("/reports", reports_router, tags=["reports"])
api.add_router("/provider", provider_router, tags=["provider"])
api.add_router("/health", health_router, tags=["health"])

urlpatterns = [
    path('', api.urls),
]


# from django.urls import path
# from ninja import NinjaAPI
# from ninja_jwt.controller import NinjaJWTDefaultController
#
#
#
# api = NinjaAPI(
#     title="MetrQ API",
#     version="1.0.0",
#     description="News Analytics SaaS API",
#     urls_namespace="api"
# )
#
# # ✅ ПРАВИЛЬНО: создаем экземпляр контроллера
# # api.register_controllers(NinjaJWTDefaultController)
#
#
# # Импортируем наши роутеры
# from .routers import auth_router, dashboard_router, reports_router, provider_router, health_router
#
# # Добавляем наши роутеры
# api.add_router("/auth", auth_router, tags=["auth"])
# api.add_router("/dashboard", dashboard_router, tags=["dashboard"])
# api.add_router("/reports", reports_router, tags=["reports"])
# api.add_router("/provider", provider_router, tags=["provider"])
# api.add_router("/health", health_router, tags=["health"])
#
# urlpatterns = [
#     path('', api.urls),
# ]


# api = NinjaAPI(title="MetrQ API", version="1.0.0")
# api.add_router("/auth", NinjaJWTDefaultController)
#
# # Импорт роутеров
# from .routers import auth, dashboard, reports, provider, health
#
# api.add_router("/auth", auth.router)
# api.add_router("/dashboard", dashboard.router)
# api.add_router("/reports", reports.router)
# api.add_router("/provider", provider.router)
# api.add_router("/health", health.router)
#
# urlpatterns = [path('', api.urls)]
