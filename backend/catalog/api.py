from django.conf import settings
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.security import django_auth
from ninja.utils import check_csrf

from accounts.schemas import MessageOut
from config.network import get_client_ip
from config.throttling import auth_throttles, view_event_throttles

from . import services
from .models import AnimeDescription
from .schemas import AnimeListOut, AnimeStatsOut, RatingIn, RatingOut

router = Router(tags=["catalog"])

WRITE_THROTTLES = auth_throttles(settings.API_WRITE_THROTTLE, settings.API_WRITE_THROTTLE_SUSTAINED)
VIEW_THROTTLES = view_event_throttles(
    settings.API_VIEW_THROTTLE, settings.API_VIEW_THROTTLE_SUSTAINED
)


@router.get("/anime", response=list[AnimeListOut])
def list_anime(request):
    return services.anime_list()


@router.get("/anime/{slug}", response={200: AnimeStatsOut, 404: MessageOut})
def anime_stats(request, slug: str):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)

    user_rating = None
    if request.user.is_authenticated:
        rating = request.user.anime_ratings.filter(anime=anime).first()
        user_rating = rating.rating if rating else None

    return {
        "slug": anime.slug,
        "avg_rating": services.average_rating(anime),
        "total_views": anime.total_views,
        "user_rating": user_rating,
    }


@router.post(
    "/anime/{slug}/view",
    response={204: None, 403: MessageOut, 404: MessageOut, 429: MessageOut},
    throttle=VIEW_THROTTLES,
)
def register_view(request, slug: str):
    if check_csrf(request) is not None:
        return 403, {"detail": "Проверка CSRF не пройдена"}

    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)
    services.register_view_event(
        anime=anime,
        user=request.user,
        ip_address=get_client_ip(request),
    )
    return 204, None


@router.post(
    "/anime/{slug}/rating",
    response={200: RatingOut, 400: MessageOut},
    auth=django_auth,
    throttle=WRITE_THROTTLES,
)
def rate_anime(request, slug: str, payload: RatingIn):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)

    try:
        avg_rating = services.rate_anime(user=request.user, anime=anime, rating=payload.rating)
    except ValueError as error:
        return 400, {"detail": str(error)}

    return 200, {
        "avg_rating": round(avg_rating, 1) if avg_rating else 0,
        "user_rating": payload.rating,
    }
