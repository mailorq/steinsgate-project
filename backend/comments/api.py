from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.security import django_auth

from accounts.schemas import MessageOut
from catalog.models import AnimeDescription
from config.throttling import auth_throttles

from . import services
from .models import Comment, CommentLike
from .schemas import CommentIn, CommentOut, CommentPageOut, ReactionIn, ReactionOut

router = Router(tags=["comments"])

WRITE_THROTTLES = auth_throttles(settings.API_WRITE_THROTTLE, settings.API_WRITE_THROTTLE_SUSTAINED)

COMMENTS_PER_PAGE = 6


def serialize_comment(comment, my_reaction: str | None, can_delete: bool) -> dict:
    profile = comment.user.profile
    return {
        "id": comment.id,
        "author": {
            "username": comment.user.username,
            "nickname": profile.nickname or comment.user.username,
            "avatar_url": profile.avatar.url if profile.avatar else None,
        },
        "text": comment.text,
        "created_at": comment.created_at,
        "likes": comment.likes,
        "dislikes": comment.dislikes,
        "my_reaction": my_reaction,
        "can_delete": can_delete,
    }


def my_reactions_map(user, comments) -> dict[int, str]:
    if not user.is_authenticated:
        return {}
    reactions = CommentLike.objects.filter(
        user=user, comment_id__in=[comment.id for comment in comments]
    ).values_list("comment_id", "is_like")
    return {comment_id: "like" if is_like else "dislike" for comment_id, is_like in reactions}


@router.get("/anime/{slug}/comments", response=CommentPageOut)
def list_comments(request, slug: str, page: int = 1):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)

    paginator = Paginator(services.comments_for_anime(anime), COMMENTS_PER_PAGE)
    page_obj = paginator.get_page(page)
    reactions = my_reactions_map(request.user, page_obj.object_list)

    return {
        "items": [
            serialize_comment(
                comment,
                reactions.get(comment.id),
                services.can_delete(user=request.user, comment=comment),
            )
            for comment in page_obj.object_list
        ],
        "page": page_obj.number,
        "total_pages": paginator.num_pages,
        "total": paginator.count,
    }


@router.post(
    "/anime/{slug}/comments",
    response={201: CommentOut, 400: MessageOut},
    auth=django_auth,
    throttle=WRITE_THROTTLES,
)
def create_comment(request, slug: str, payload: CommentIn):
    anime = get_object_or_404(AnimeDescription.refs(), slug=slug)

    try:
        comment = services.create_comment(user=request.user, anime=anime, text=payload.text)
    except services.CommentRejected as error:
        return 400, {"detail": str(error)}

    comment.likes = 0
    comment.dislikes = 0
    return 201, serialize_comment(comment, None, True)


@router.delete(
    "/comments/{comment_id}",
    response={204: None, 403: MessageOut},
    auth=django_auth,
)
def delete_comment(request, comment_id: int):
    comment = get_object_or_404(Comment, id=comment_id)

    if not services.can_delete(user=request.user, comment=comment):
        return 403, {"detail": "Недостаточно прав для удаления"}

    services.delete_comment(user=request.user, comment=comment)
    return 204, None


@router.post(
    "/comments/{comment_id}/reaction",
    response=ReactionOut,
    auth=django_auth,
    throttle=WRITE_THROTTLES,
)
def toggle_reaction(request, comment_id: int, payload: ReactionIn):
    comment = get_object_or_404(Comment, id=comment_id)
    return services.toggle_reaction(
        user=request.user, comment=comment, is_like=payload.is_like
    )
