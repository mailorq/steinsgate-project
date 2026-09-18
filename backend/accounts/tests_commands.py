import os
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Count, Q
from django.test import TestCase
from django.utils import timezone

from accounts.models import EmailDeliveryQuota, EmailVerificationCode, email_delivery_fingerprint
from catalog import services as catalog_services
from catalog.models import AnimeDescription, ViewHistory
from comments.models import Comment, CommentLike


class SeedLoadtestCommandTest(TestCase):

    def test_seed_is_idempotent(self):
        call_command("seed_loadtest", "--force", "--users", "5", "--comments", "3", stdout=StringIO())
        first = User.objects.filter(username__startswith="loadtest_").count()
        call_command("seed_loadtest", "--force", "--users", "5", "--comments", "3", stdout=StringIO())
        second = User.objects.filter(username__startswith="loadtest_").count()
        self.assertEqual(first, 5)
        self.assertEqual(second, 5)

    def test_seed_keeps_counters_consistent_with_rows(self):
        call_command("seed_loadtest", "--force", "--users", "6", "--comments", "4", stdout=StringIO())

        comments = Comment.objects.annotate(
            likes=Count("comment_likes", filter=Q(comment_likes__is_like=True)),
            dislikes=Count("comment_likes", filter=Q(comment_likes__is_like=False)),
        )
        for comment in comments:
            self.assertEqual((comment.likes_count, comment.dislikes_count), (comment.likes, comment.dislikes))
        for anime in AnimeDescription.objects.annotate(rows=Count("views")):
            self.assertEqual(anime.total_views, anime.rows)

    def test_flush_removes_users_and_generated_views(self):
        call_command("seed_loadtest", "--force", "--users", "4", "--comments", "2", stdout=StringIO())
        user = User.objects.filter(username__startswith="loadtest_").first()
        anime = AnimeDescription.objects.first()
        catalog_services.register_view_event(anime=anime, user=user, ip_address="10.9.9.9")

        call_command("seed_loadtest", "--force", "--flush", stdout=StringIO())

        self.assertEqual(User.objects.filter(username__startswith="loadtest_").count(), 0)
        self.assertEqual(Comment.objects.count(), 0)
        self.assertFalse(ViewHistory.objects.exists())
        self.assertFalse(AnimeDescription.objects.exclude(total_views=0).exists())

    def test_reactions_only_on_loadtest_comments(self):
        anime = AnimeDescription.objects.first()
        real_user = User.objects.create_user(username="okabe", password="x")
        real_comment = Comment.objects.create(anime=anime, user=real_user, text="реальный комментарий")

        call_command("seed_loadtest", "--force", "--users", "5", "--comments", "3", stdout=StringIO())

        self.assertEqual(real_comment.comment_likes.count(), 0)

    def test_guard_requires_marker(self):
        with mock.patch.dict(os.environ, {"LOADTEST": ""}):
            with self.assertRaises(CommandError):
                call_command("seed_loadtest", "--users", "1", stdout=StringIO())


class RecountReactionsCommandTest(TestCase):

    def test_drifted_counters_are_restored(self):
        anime = AnimeDescription.objects.first()
        author = User.objects.create_user(username="okabe", password="x")
        comment = Comment.objects.create(
            anime=anime, user=author, text="реальный комментарий", likes_count=7
        )
        CommentLike.objects.create(user=author, comment=comment, is_like=True)

        call_command("recount_reactions", stdout=StringIO())

        comment.refresh_from_db()
        self.assertEqual((comment.likes_count, comment.dislikes_count), (1, 0))


class ProfileQueriesCommandTest(TestCase):

    def test_runs_rolls_back_and_clears_cache(self):
        out = StringIO()
        call_command("profile_queries", "--force", stdout=out)

        self.assertFalse(AnimeDescription.objects.filter(slug__startswith="profile-").exists())
        self.assertIsNone(cache.get("catalog:anime_list"))
        self.assertIn("N+1", out.getvalue())


class PurgeExpiredRegistrationsCommandTest(TestCase):
    def _pending_user(self, username: str, *, expired: bool) -> User:
        user = User.objects.create_user(
            username=username,
            email=f"{username}@gmail.com",
            password="complex_pass_123",
            is_active=False,
        )
        record = EmailVerificationCode(user=user)
        record.rotate_code()
        if expired:
            record.created_at = timezone.now() - EmailVerificationCode.TTL - timedelta(seconds=1)
        record.save()
        return user

    def test_dry_run_leaves_records_untouched(self):
        user = self._pending_user("expired", expired=True)

        call_command("purge_expired_registrations", "--dry-run", stdout=StringIO())

        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_removes_only_expired_pending_registrations(self):
        expired = self._pending_user("expired", expired=True)
        current = self._pending_user("current", expired=False)
        disabled = User.objects.create_user(
            username="disabled", email="disabled@gmail.com", password="complex_pass_123", is_active=False
        )

        call_command("purge_expired_registrations", stdout=StringIO())

        self.assertFalse(User.objects.filter(pk=expired.pk).exists())
        self.assertTrue(User.objects.filter(pk=current.pk).exists())
        self.assertTrue(User.objects.filter(pk=disabled.pk).exists())

    def test_removes_only_expired_delivery_quotas(self):
        expired = EmailDeliveryQuota.objects.create(
            email_fingerprint=email_delivery_fingerprint("expired@gmail.com"),
            window_started_at=timezone.now() - EmailDeliveryQuota.WINDOW - timedelta(seconds=1),
        )
        current = EmailDeliveryQuota.objects.create(
            email_fingerprint=email_delivery_fingerprint("current@gmail.com"),
        )

        call_command("purge_expired_registrations", stdout=StringIO())

        self.assertFalse(EmailDeliveryQuota.objects.filter(pk=expired.pk).exists())
        self.assertTrue(EmailDeliveryQuota.objects.filter(pk=current.pk).exists())
