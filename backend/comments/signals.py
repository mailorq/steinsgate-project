from django.conf import settings
from django.db.models import F
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Comment, CommentLike


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL)
def release_reactions_of_deleted_user(sender, instance, **kwargs):
    # реакции уходят каскадом в обход toggle_reaction, поэтому счетчики уменьшаются здесь
    # строка пользователя берется первой, как в toggle_reaction, и его реакции ждут конца удаления
    sender.objects.select_for_update().filter(pk=instance.pk).exists()
    reactions = CommentLike.objects.filter(user=instance)
    locked_comments = (
        Comment.objects.select_for_update()
        .filter(pk__in=reactions.values("comment"))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if not list(locked_comments):
        return
    Comment.objects.filter(pk__in=reactions.filter(is_like=True).values("comment")).update(
        likes_count=F("likes_count") - 1
    )
    Comment.objects.filter(pk__in=reactions.filter(is_like=False).values("comment")).update(
        dislikes_count=F("dislikes_count") - 1
    )
