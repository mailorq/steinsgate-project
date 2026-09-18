from django.core.management.base import BaseCommand

from comments.models import Comment
from comments.services import recount_reactions


class Command(BaseCommand):
    help = "Пересчитывает счетчики лайков и дизлайков по таблице реакций"

    def add_arguments(self, parser):
        parser.add_argument("--anime", help="слаг тайтла, по умолчанию пересчитываются все")

    def handle(self, *args, **options):
        comments = Comment.objects.all()
        if options["anime"]:
            comments = comments.filter(anime__slug=options["anime"])

        updated = recount_reactions(comments)
        self.stdout.write(self.style.SUCCESS(f"Пересчитано комментариев: {updated}"))
