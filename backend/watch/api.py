from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.security import django_auth

from catalog.models import AnimeDescription

from . import services
from .schemas import ProgressIn, ProgressOut

router = Router(tags=["watch"])


@router.get("/anime/{slug}/progress", response=ProgressOut, auth=django_auth)
def get_progress(request, slug: str):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)
    progress = request.user.watch_progress.filter(anime=anime).first()

    if progress is None:
        return {"current_time": 0, "duration": 0, "percentage": 0}

    return {
        "current_time": progress.current_time,
        "duration": progress.duration,
        "percentage": progress.progress_percentage,
    }


@router.put("/anime/{slug}/progress", response=ProgressOut, auth=django_auth)
def save_progress(request, slug: str, payload: ProgressIn):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)
    progress = services.save_progress(
        user=request.user,
        anime=anime,
        current_time=payload.current_time,
        duration=payload.duration,
    )

    return {
        "current_time": progress.current_time,
        "duration": progress.duration,
        "percentage": progress.progress_percentage,
    }
