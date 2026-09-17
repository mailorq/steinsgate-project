import logging
import re

import bleach
from django.db import transaction
from django.db.models import Count, F, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce

from . import moderation
from .models import Comment, CommentLike

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("security")

URL_PATTERN = r'(https?://\S+|www\.\S+|\w+\.(com|ru|net|org|tk|xyz|bit|cc))'
MIN_LENGTH = 3
MAX_LENGTH = 900


class CommentRejected(Exception):
    pass


def create_comment(*, user, anime, text: str) -> Comment:
    cleaned = bleach.clean(text.strip(), tags=[], strip=True)
    # Спойлер-маркеры не участвуют в проверках содержимого
    plain = cleaned.replace("||", "")

    if re.search(URL_PATTERN, plain, re.IGNORECASE):
        security_logger.warning(f"Spam attempt blocked, user={user.username}, anime={anime.slug}")
        raise CommentRejected("Ссылки в комментариях запрещены")

    if not (MIN_LENGTH <= len(plain) <= MAX_LENGTH):
        raise CommentRejected("Недопустимая длина комментария")

    rejection = moderation.check_comment(plain)
    if rejection is not None:
        security_logger.warning(
            f"Comment rejected by moderation, user={user.username}, anime={anime.slug}, reason={rejection}"
        )
        raise CommentRejected(rejection)

    comment = Comment.objects.create(user=user, anime=anime, text=cleaned)
    logger.info(f"Comment {comment.id} created, anime={anime.slug}, user={user.username}, length={len(cleaned)}")
    return comment


def can_delete(*, user, comment: Comment) -> bool:
    if not user.is_authenticated:
        return False
    return user.is_superuser or user.is_staff or comment.user_id == user.id


def delete_comment(*, user, comment: Comment) -> None:
    comment_id, author = comment.id, comment.user.username
    comment.delete()
    logger.info(f"Comment {comment_id} deleted, by={user.username}, author={author}")


def comments_for_anime(anime):
    return (
        anime.comments
        .select_related("user", "user__profile")
        .order_by("-created_at", "-id")
    )


def toggle_reaction(*, user, comment_id: int, is_like: bool) -> dict | None:
    counter, opposite = (
        ("likes_count", "dislikes_count") if is_like else ("dislikes_count", "likes_count")
    )
    with transaction.atomic():
        # блокировка комментария сериализует реакции на него: решение по текущей реакции и изменение счетчиков не расходятся при параллельных запросах
        comment = Comment.objects.select_for_update().filter(pk=comment_id).first()
        if comment is None:
            return None

        reaction = CommentLike.objects.filter(user=user, comment=comment).first()
        if reaction is None:
            CommentLike.objects.create(user=user, comment=comment, is_like=is_like)
            deltas = {counter: F(counter) + 1}
        elif reaction.is_like == is_like:
            reaction.delete()
            deltas = {counter: F(counter) - 1}
        else:
            reaction.is_like = is_like
            reaction.save(update_fields=["is_like"])
            deltas = {counter: F(counter) + 1, opposite: F(opposite) - 1}

        Comment.objects.filter(pk=comment.pk).update(**deltas)
        comment.refresh_from_db(fields=["likes_count", "dislikes_count"])

    return {
        "likes": comment.likes_count,
        "dislikes": comment.dislikes_count,
        "rating": comment.likes_count - comment.dislikes_count,
    }


def _reaction_total(is_like: bool):
    per_comment = (
        CommentLike.objects.filter(comment=OuterRef("pk"), is_like=is_like)
        .order_by()
        .values("comment")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return Coalesce(Subquery(per_comment), Value(0), output_field=IntegerField())


def recount_reactions(comments) -> int:
    """пересчитывает счетчики по реакциям для записей, созданных в обход сервиса"""
    return comments.update(
        likes_count=_reaction_total(True), dislikes_count=_reaction_total(False)
    )
