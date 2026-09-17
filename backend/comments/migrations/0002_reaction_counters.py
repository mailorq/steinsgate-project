from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce


def count_existing_reactions(apps, schema_editor):
    Comment = apps.get_model("comments", "Comment")
    CommentLike = apps.get_model("comments", "CommentLike")

    def total(is_like):
        per_comment = (
            CommentLike.objects.filter(comment=OuterRef("pk"), is_like=is_like)
            .order_by()
            .values("comment")
            .annotate(total=Count("pk"))
            .values("total")
        )
        return Coalesce(
            Subquery(per_comment), Value(0), output_field=models.PositiveIntegerField()
        )

    Comment.objects.update(likes_count=total(True), dislikes_count=total(False))


class Migration(migrations.Migration):

    dependencies = [
        ("comments", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="comment",
            name="likes_count",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="comment",
            name="dislikes_count",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.RunPython(count_existing_reactions, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["anime", "-created_at", "-id"], name="comments_page_idx"),
        ),
    ]
