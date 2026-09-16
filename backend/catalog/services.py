import hashlib
import logging
import math
from datetime import timedelta

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Avg
from django.utils import timezone

from .models import AnimeDescription, AnimeRating, ViewHistory

logger = logging.getLogger(__name__)

ANIME_LIST_TTL = 300

VIEW_DEDUP_WINDOW = timedelta(hours=24)

# Средний рейтинг инвалидируется точечно при новой оценке; счётчик просмотров
# живёт по TTL — отставание на минуту для витрины безразлично
AVG_RATING_TTL = 3600
VIEWS_COUNT_TTL = 60


def _avg_rating_key(anime) -> str:
    return f"anime:{anime.id}:avg_rating"


def _views_key(anime) -> str:
    return f"anime:{anime.id}:views"


def _view_dedup_key(anime, viewer, ip_address: str | None) -> str:
    identity = f"user:{viewer.pk}" if viewer is not None else f"ip:{ip_address or 'unknown'}"
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    return f"view-dedup:{anime.pk}:{fingerprint}"


def _acquire_view_dedup_lock(*, anime, viewer, ip_address: str | None) -> None:

    if connection.vendor != "postgresql":
        return

    identity = f"user:{viewer.pk}" if viewer is not None else f"ip:{ip_address or 'unknown'}"
    lock_material = f"{anime.pk}:{identity}".encode()
    lock_key = int.from_bytes(
        hashlib.blake2b(lock_material, digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


def _recent_view(*, anime, viewer, ip_address, since):
    recent = ViewHistory.objects.filter(anime=anime, viewed_at__gte=since)
    if viewer is not None:
        recent = recent.filter(user=viewer)
    else:
        recent = recent.filter(user__isnull=True, ip_address=ip_address)
    return recent.first()


def _cache_remaining_dedup_window(*, key: str, viewed_at, now) -> None:
    remaining = math.ceil((viewed_at + VIEW_DEDUP_WINDOW - now).total_seconds())
    if remaining > 0:
        _safe_cache(cache.set, key, "1", remaining)


def register_view_event(*, anime, user, ip_address):
    viewer = user if user is not None and user.is_authenticated else None
    dedup_key = _view_dedup_key(anime, viewer, ip_address)
    if _safe_cache(cache.get, dedup_key) is not None:
        return None

    with transaction.atomic():
        _acquire_view_dedup_lock(anime=anime, viewer=viewer, ip_address=ip_address)
        now = timezone.now()
        since = now - VIEW_DEDUP_WINDOW
        recent = _recent_view(
            anime=anime, viewer=viewer, ip_address=ip_address, since=since
        )
        if recent is not None:
            transaction.on_commit(
                lambda: _cache_remaining_dedup_window(
                    key=dedup_key, viewed_at=recent.viewed_at, now=timezone.now()
                )
            )
            return None

        view = ViewHistory.objects.create(anime=anime, user=viewer, ip_address=ip_address)
        # Cache changes happen only after the database write is durable. This
        # also handles callers that wrap the service in an outer transaction.
        transaction.on_commit(
            lambda: _cache_remaining_dedup_window(
                key=dedup_key, viewed_at=view.viewed_at, now=timezone.now()
            )
        )
        transaction.on_commit(lambda: _safe_cache(cache.delete, _views_key(anime)))
        return view


def rate_anime(*, user, anime, rating: int) -> float | None:
    if not 1 <= rating <= 5:
        raise ValueError("Оценка должна быть от 1 до 5")

    AnimeRating.objects.update_or_create(
        user=user, anime=anime, defaults={"rating": rating}
    )
    value = _compute_average(anime)
    _safe_cache(cache.set, _avg_rating_key(anime), _encode_average(value), AVG_RATING_TTL)
    return value


def _compute_average(anime) -> float | None:
    return anime.ratings.aggregate(Avg("rating"))["rating__avg"]


def _encode_average(value: float | None):
    return value if value is not None else "none"


def average_rating(anime) -> float | None:
    key = _avg_rating_key(anime)
    cached = _safe_cache(cache.get, key)
    if cached is not None:
        return cached if cached != "none" else None

    value = _compute_average(anime)
    # add, а не set: чтение, посчитанное до коммита голоса, не перетирает свежее значение
    _safe_cache(cache.add, key, _encode_average(value), AVG_RATING_TTL)
    return value


def anime_list() -> list[dict]:
    key = "catalog:anime_list"
    cached = _safe_cache(cache.get, key)
    if cached is not None:
        return cached

    value = list(AnimeDescription.objects.values("slug", "name"))
    _safe_cache(cache.set, key, value, ANIME_LIST_TTL)
    return value


def total_views(anime) -> int:
    key = _views_key(anime)
    cached = _safe_cache(cache.get, key)
    if cached is not None:
        return cached

    value = anime.views.count()
    _safe_cache(cache.set, key, value, VIEWS_COUNT_TTL)
    return value


def _safe_cache(operation, *args):
    try:
        return operation(*args)
    except Exception:
        logger.exception("Cache unavailable, falling back to database")
        return None
