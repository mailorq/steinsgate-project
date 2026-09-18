from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce


def count_existing_views(apps, schema_editor):
    AnimeDescription = apps.get_model("catalog", "AnimeDescription")
    ViewHistory = apps.get_model("catalog", "ViewHistory")
    per_title = (
        ViewHistory.objects.filter(anime=OuterRef("pk"))
        .order_by()
        .values("anime")
        .annotate(total=Count("pk"))
        .values("total")
    )
    AnimeDescription.objects.update(
        total_views=Coalesce(
            Subquery(per_title), Value(0), output_field=models.PositiveBigIntegerField()
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_viewhistory_catalog_view_user_idx_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="animedescription",
            name="total_views",
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
        migrations.RunPython(count_existing_views, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="viewhistory",
            name="catalog_view_history_idx",
        ),
        migrations.AddIndex(
            model_name="viewhistory",
            index=models.Index(fields=["viewed_at"], name="catalog_view_rotation_idx"),
        ),
    ]
