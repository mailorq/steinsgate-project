import logging
import math

from django.conf import settings
from ninja import NinjaAPI
from ninja.errors import Throttled

from accounts.api import auth_router, profile_router
from catalog.api import router as catalog_router
from comments.api import router as comments_router
from watch.api import router as watch_router

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="SteinsGate API",
    version="2.0.0",
    # docs_url прячет только Swagger UI, сама схема живет на openapi_url
    docs_url="/docs" if settings.API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if settings.API_DOCS_ENABLED else None,
)


@api.exception_handler(Throttled)
def throttled(request, exc):
    if getattr(request, "_security_throttle_unavailable", False):
        logger.error("Security request rejected because throttle storage is unavailable")
        return api.create_response(
            request,
            {"detail": "Сервис временно недоступен. Попробуйте позже."},
            status=503,
        )

    response = api.create_response(request, {"detail": "Слишком много запросов."}, status=429)
    if exc.wait is not None:
        response["Retry-After"] = str(max(math.ceil(exc.wait), 1))
    return response

api.add_router("/auth", auth_router)
api.add_router("/profile", profile_router)
api.add_router("", catalog_router)
api.add_router("", comments_router)
api.add_router("", watch_router)
