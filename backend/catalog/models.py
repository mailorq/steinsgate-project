from django.conf import settings
from django.db import models


class AnimeDescription(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    appearing = models.CharField(max_length=100)
    type = models.CharField(max_length=100)
    genres = models.CharField(max_length=500)
    description = models.TextField()
    poster = models.ImageField(upload_to="posters/", blank=True, default="")
    total_views = models.PositiveBigIntegerField(default=0, db_default=0)

    def __str__(self):
        return self.name

    @classmethod
    def refs(cls):
        return cls.objects.only("id", "slug", "total_views")


class AnimeRating(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="anime_ratings"
    )
    anime = models.ForeignKey(
        AnimeDescription, on_delete=models.CASCADE, related_name="ratings"
    )
    rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "anime"], name="unique_user_anime_rating"),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.anime.name} - {self.rating}"


class ViewHistory(models.Model):
    anime = models.ForeignKey(
        AnimeDescription, on_delete=models.CASCADE, related_name="views"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="view_history",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-viewed_at"]
        # порядок полей повторяет фильтры дедупликации, чтобы first() брал первую строку индекса вместо сортировки
        indexes = [
            models.Index(fields=["anime", "user", "-viewed_at"], name="catalog_view_user_idx"),
            models.Index(fields=["anime", "ip_address", "-viewed_at"], name="catalog_view_ip_idx"),
            models.Index(fields=["viewed_at"], name="catalog_view_rotation_idx"),
        ]

    def __str__(self):
        return f"{self.anime.name} - {self.viewed_at}"
