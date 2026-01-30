from django.http import JsonResponse
from django.conf import settings


class CORSMiddleware:
    """CORS handling middleware"""

    ALLOWED_ORIGINS = getattr(settings, 'CORS_ALLOWED_ORIGINS', [
        "https://metrq.onrender.com",
        "http://localhost:3000",
    ])

    ALLOWED_METHODS = "GET, POST, OPTIONS, PATCH"
    ALLOWED_HEADERS = "authorization, content-type, x-api-key"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Handle preflight requests
        if request.method == "OPTIONS":
            response = JsonResponse({}, status=200)
            self._add_cors_headers(request, response)
            return response

        response = self.get_response(request)
        self._add_cors_headers(request, response)
        return response

    def _add_cors_headers(self, request, response):
        origin = request.headers.get('Origin', '')

        if origin in self.ALLOWED_ORIGINS:
            response["Access-Control-Allow-Origin"] = origin
        else:
            response["Access-Control-Allow-Origin"] = "https://metrq.onrender.com"

        response["Access-Control-Allow-Methods"] = self.ALLOWED_METHODS
        response["Access-Control-Allow-Headers"] = self.ALLOWED_HEADERS
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Max-Age"] = "86400"  # 24 hours
