from django.contrib import admin

from .models import AnimeDescription, AnimeRating, ViewHistory


@admin.register(AnimeDescription)
class AnimeDescriptionAdmin(admin.ModelAdmin):
    readonly_fields = ("total_views",)

    def save_model(self, request, obj, form, change):
        # полный UPDATE записал бы total_views, прочитанный до параллельных просмотров
        obj.save(update_fields=form.changed_data if change else None)


admin.site.register(AnimeRating)
admin.site.register(ViewHistory)
