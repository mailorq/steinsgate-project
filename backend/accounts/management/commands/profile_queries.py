"""
детектор n+1: считает число sql запросов на горячих read-ручках

в отличие от нагрузочного прогона (сколько тормозит) отвечает на вопрос
«почему»: если число запросов на странице растет вместе с числом выводимых
комментариев - это n+1, лечится select_related/prefetch_related

данные создаются во временной транзакции и откатываются - живая бд не меняется
тест клиент бьет по реальному URLconf, запущенный сервер не нужен

LOADTEST=1 python manage.py profile_queries
"""

import os

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from catalog.models import AnimeDescription, AnimeRating, ViewHistory
from comments.models import Comment, CommentLike


class Command(BaseCommand):
    help = "Считает число SQL-запросов на горячих ручках и ищет N+1"

    def add_arguments(self, parser):
        parser.add_argument("--verbose-sql", action="store_true", help="Печатать сами запросы")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        if os.environ.get("LOADTEST") != "1" and not options["force"]:
            raise CommandError("Запустите с LOADTEST=1 (создаёт временные данные, откат гарантирован).")

        self.verbose_sql = options["verbose_sql"]
        try:
            with transaction.atomic():
                self._run()
                transaction.set_rollback(True)
        finally:
            cache.delete("catalog:anime_list")

    def _run(self):
        # транзакция откатывает бд, но не redis: временные слаги иначе оседают в catalog:anime_list на весь ttl
        cache.delete("catalog:anime_list")

        user = User.objects.create_user(username="__profile_probe__", password="x", is_active=True)
        other = User.objects.create_user(username="__profile_other__", password="x", is_active=True)

        few = self._make_anime("profile-few", comments=3, author=other)
        full = self._make_anime("profile-full", comments=6, author=other)
        # оба тайтла показывают одну страницу (6 на странице), но few дает 3 комментария, full 6
        # если запросов на full заметно больше - n+1.

        # ALLOWED_HOSTS содержит localhost; тест-клиент по умолчанию ходит с
        # testserver, которого там нет (его добавляет только TestCase)
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)

        self.stdout.write(self.style.MIGRATE_HEADING("\nЧисло SQL-запросов по ручкам:\n"))
        header = f"{'ручка':<42}{'запросов':>10}"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        n_list = self._measure(client, "GET /api/anime", "/api/anime")
        # второй вызов каталога - проверка, что кэш убирает запросы
        n_list2 = self._measure(client, "GET /api/anime (повтор, кэш)", "/api/anime")
        self._measure(client, "GET /api/anime/[slug]", f"/api/anime/{full.slug}")
        n_few = self._measure(
            client, "GET .../comments (3 коммента)", f"/api/anime/{few.slug}/comments?page=1"
        )
        n_full = self._measure(
            client, "GET .../comments (6 комментов)", f"/api/anime/{full.slug}/comments?page=1"
        )

        self._verdict(n_list, n_list2, n_few, n_full)

        for a in (few, full):
            cache.delete(f"anime:{a.id}:avg_rating")

    def _make_anime(self, slug: str, *, comments: int, author: User) -> AnimeDescription:
        anime = AnimeDescription.objects.create(
            name=f"Probe {slug}",
            slug=slug,
            appearing="1",
            type="ТВ",
            genres="sci-fi",
            description="probe",
        )
        made = Comment.objects.bulk_create(
            [Comment(anime=anime, user=author, text=f"probe comment {i}") for i in range(comments)]
        )
        # немного реакций и оценка - воспроизводим реальную страницу
        CommentLike.objects.bulk_create(
            [CommentLike(user=author, comment=c, is_like=True) for c in made]
        )
        AnimeRating.objects.create(user=author, anime=anime, rating=5)
        ViewHistory.objects.create(anime=anime, user=author, ip_address="203.0.113.0")
        return anime

    def _measure(self, client: Client, label: str, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)
        count = len(ctx.captured_queries)
        status = response.status_code
        flag = "" if status == 200 else f"  [status {status}]"
        self.stdout.write(f"{label:<42}{count:>10}{flag}")
        if self.verbose_sql:
            for q in ctx.captured_queries:
                self.stdout.write(f"    {q['sql'][:140]}")
        return count

    def _verdict(self, n_list, n_list2, n_few, n_full):
        self.stdout.write("")
        if n_list2 < n_list:
            self.stdout.write(self.style.SUCCESS(f"Кэш каталога: {n_list} -> {n_list2} запросов, работает."))
        else:
            self.stdout.write(self.style.WARNING(f"Кэш каталога не сократил запросы ({n_list} -> {n_list2})."))

        delta = n_full - n_few
        # 3 лишних комментария на странице. рост запросов ~на число комментов = n+1.
        if delta >= 3:
            self.stdout.write(
                self.style.ERROR(
                    f"N+1 на комментариях: 3 коммента = {n_few} запросов, "
                    f"6 комментов = {n_full} (+{delta}). Запросы растут с числом строк."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Комментарии без N+1: {n_few} vs {n_full} запросов (+{delta}) — "
                    "число запросов почти не зависит от числа комментариев."
                )
            )
