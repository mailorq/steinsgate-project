"""
наполнение базы данными для нагрузочного теста

работает только под маркером LOADTEST=1 (боевое окружение его не выставляет)
и оперирует исключительно объектами с префиксом loadtest_, поэтому живые
данные не затрагиваются ни при создании, ни при --flush

LOADTEST=1 python manage.py seed_loadtest --users 500 --comments 150
LOADTEST=1 python manage.py seed_loadtest --flush
"""

import os
import random

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, F, Q

from catalog.models import AnimeDescription, AnimeRating, ViewHistory
from comments.models import Comment, CommentLike
from comments.services import recount_reactions

USERNAME_PREFIX = "loadtest_"
DEFAULT_PASSWORD = "loadtest-pass-12345"
EMAIL_DOMAIN = "@gmail.com"

COMMENT_SENTENCES = (
    "Эль псай конгру, эта серия перевернула всё.",
    "Курису лучший научный ассистент, спорить бесполезно.",
    "Смотрю второй раз и всё равно мурашки от финала.",
    "Мировые линии сходятся, а я всё ещё не готов.",
    "Окабе снова тащит на себе весь сюжет.",
    "Тайм-лайн дивергенции наконец обрёл смысл.",
    "Маюри, пожалуйста, больше так не делай.",
    "Дару как всегда добавляет нужную разрядку.",
    "Пожалуй, лучшая арка во всём тайтле.",
    "СЕРН опять всё испортил, классика.",
)


class Command(BaseCommand):
    help = "Наполняет БД синтетическими данными loadtest_* для нагрузочного теста"

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=500, help="Сколько тест-юзеров создать")
        parser.add_argument(
            "--comments", type=int, default=150, help="Комментариев на каждый тайтл"
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Удалить все loadtest_* объекты и выйти",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Обойти проверку маркера LOADTEST=1 (осознанно)",
        )

    def handle(self, *args, **options):
        if os.environ.get("LOADTEST") != "1" and not options["force"]:
            raise CommandError(
                "Отказ: команда меняет данные. Запустите с LOADTEST=1 "
                "(или --force, если осознаёте, против какой БД работаете)."
            )

        if options["flush"]:
            self._flush()
            return

        self._seed(users=options["users"], comments_per_title=options["comments"])

    @transaction.atomic
    def _flush(self):
        qs = User.objects.filter(username__startswith=USERNAME_PREFIX)
        count = qs.count()

        # ViewHistory.user = SET_NULL: если сначала удалить юзеров, их просмотры
        # осиротеют и потеряют признак принадлежности. поэтому удаляем просмотры
        # тест-юзеров до самих юзеров, пока FK еще указывает на них
        seeded_views = ViewHistory.objects.filter(
            Q(user__username__startswith=USERNAME_PREFIX) | Q(ip_address="203.0.113.0")
        )
        # total_views не пересчитывается из истории: вычитаем ровно удаляемые строки
        for row in seeded_views.order_by().values("anime").annotate(total=Count("pk")):
            AnimeDescription.objects.filter(pk=row["anime"]).update(
                total_views=F("total_views") - row["total"]
            )
        views_deleted, _ = seeded_views.delete()

        # комментарии, реакции и оценки уйдут каскадом вместе с юзерами
        qs.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Удалено тест-юзеров: {count}, просмотров: {views_deleted}"
            )
        )

    @transaction.atomic
    def _seed(self, *, users: int, comments_per_title: int):
        titles = list(AnimeDescription.objects.all())
        if not titles:
            raise CommandError(
                "Каталог пуст. Сначала примените миграции (catalog/0002_seed_titles)."
            )

        created_users = self._seed_users(users)
        pool = list(User.objects.filter(username__startswith=USERNAME_PREFIX))
        if not pool:
            raise CommandError("Пул тест-юзеров пуст после сидинга.")

        self._seed_comments(titles, pool, comments_per_title)
        self._seed_ratings(titles, pool)
        self._seed_views(titles, pool)

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: юзеров +{created_users} (всего в пуле {len(pool)}), "
                f"тайтлов {len(titles)}, пароль у всех: {DEFAULT_PASSWORD}"
            )
        )

    def _seed_users(self, users: int) -> int:
        existing = set(
            User.objects.filter(username__startswith=USERNAME_PREFIX).values_list(
                "username", flat=True
            )
        )
        to_create = []
        for i in range(users):
            username = f"{USERNAME_PREFIX}user_{i}"
            if username in existing:
                continue
            user = User(username=username, email=f"{username}{EMAIL_DOMAIN}", is_active=True)
            user.set_password(DEFAULT_PASSWORD)
            to_create.append(user)

        # bulk_create не шлет post_save, поэтому профили создаем отдельно ниже
        User.objects.bulk_create(to_create, batch_size=500)

        from accounts.models import Profile

        without_profile = User.objects.filter(
            username__startswith=USERNAME_PREFIX, profile__isnull=True
        )
        Profile.objects.bulk_create(
            [Profile(user=u, nickname=u.username) for u in without_profile],
            batch_size=500,
        )
        return len(to_create)

    def _seed_comments(self, titles, pool, per_title: int):
        for title in titles:
            existing = Comment.objects.filter(
                anime=title, user__username__startswith=USERNAME_PREFIX
            ).count()
            deficit = max(per_title - existing, 0)
            if not deficit:
                continue

            batch = [
                Comment(
                    anime=title,
                    user=random.choice(pool),
                    text=random.choice(COMMENT_SENTENCES),
                )
                for _ in range(deficit)
            ]
            Comment.objects.bulk_create(batch, batch_size=500)

            # реакции: часть комментов получает лайки/дизлайки от других юзеров
            fresh = list(
                Comment.objects.filter(
                    anime=title, user__username__startswith=USERNAME_PREFIX
                ).order_by("-id")[:deficit]
            )
            self._seed_reactions(fresh, pool)

    def _seed_reactions(self, comments, pool):
        likes = []
        seen = set()
        for comment in comments:
            reactors = random.sample(pool, k=min(len(pool), random.randint(0, 8)))
            for user in reactors:
                key = (user.id, comment.id)
                if key in seen:
                    continue
                seen.add(key)
                likes.append(
                    CommentLike(user=user, comment=comment, is_like=random.random() > 0.3)
                )
        CommentLike.objects.bulk_create(likes, batch_size=1000, ignore_conflicts=True)
        recount_reactions(Comment.objects.filter(pk__in=[comment.pk for comment in comments]))

    def _seed_ratings(self, titles, pool):
        ratings = []
        for title in titles:
            raters = random.sample(pool, k=min(len(pool), 200))
            for user in raters:
                ratings.append(
                    AnimeRating(user=user, anime=title, rating=random.randint(1, 5))
                )
        AnimeRating.objects.bulk_create(ratings, batch_size=1000, ignore_conflicts=True)

    def _seed_views(self, titles, pool):
        # у ViewHistory нет unique ограничения, поэтому идемпотентность держим сами: тайтл с уже засеянными просмотрами пропускаем
        views = []
        for title in titles:
            if ViewHistory.objects.filter(anime=title, ip_address="203.0.113.0").exists():
                continue
            viewers = random.sample(pool, k=min(len(pool), 300))
            for user in viewers:
                views.append(ViewHistory(anime=title, user=user, ip_address="203.0.113.0"))
            AnimeDescription.objects.filter(pk=title.pk).update(
                total_views=F("total_views") + len(viewers)
            )
        ViewHistory.objects.bulk_create(views, batch_size=1000)
