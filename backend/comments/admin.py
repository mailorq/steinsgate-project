from django.contrib import admin

from .models import Comment


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    readonly_fields = ("likes_count", "dislikes_count")

    def save_model(self, request, obj, form, change):
        obj.save(update_fields=form.changed_data if change else None)
