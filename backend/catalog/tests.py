from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from accounts.tests import csrf_headers

from . import services
from .models import AnimeDescription, ViewHistory

User = get_user_model()


class SeedDataTest(TestCase):

    def test_all_titles_seeded(self):
        slugs = set(AnimeDescription.objects.values_list('slug', flat=True))

        self.assertEqual(slugs, {
            'steins-gate',
            'steins-gate-zero',
            'steins-gate-load-region-of-deja-vu',
            'steins-gate-kyoukaimenjou-no-missing-link',
        })

    def test_seeded_title_fields(self):
        anime = AnimeDescription.objects.get(slug='steins-gate')

        self.assertEqual(anime.name, 'Steins;Gate')
        self.assertEqual(anime.appearing, '2011 весна')
        self.assertTrue(anime.description)


class AnimeModelTest(TestCase):

    def setUp(self):
        self.anime = AnimeDescription.objects.get(slug='steins-gate')

    def test_anime_str_representation(self):
        self.assertEqual(str(self.anime), 'Steins;Gate')

    def test_anime_has_no_comments_initially(self):
        self.assertEqual(self.anime.comments.count(), 0)


class CatalogServiceTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='okabe', password='elpsykongroo')
        self.anime = AnimeDescription.objects.get(slug='steins-gate')

    def test_view_event_deduplicated_for_user(self):
        services.register_view_event(anime=self.anime, user=self.user, ip_address='1.1.1.1')
        services.register_view_event(anime=self.anime, user=self.user, ip_address='1.1.1.1')

        self.assertEqual(ViewHistory.objects.count(), 1)

    def test_view_event_deduplicated_for_anonymous_by_ip(self):
        services.register_view_event(anime=self.anime, user=None, ip_address='2.2.2.2')
        services.register_view_event(anime=self.anime, user=None, ip_address='2.2.2.2')
        services.register_view_event(anime=self.anime, user=None, ip_address='3.3.3.3')

        self.assertEqual(ViewHistory.objects.count(), 2)

    def test_view_event_does_not_depend_on_a_redis_lease(self):
        # A finite cache lease is not allowed to decide whether an event is
        # recorded: PostgreSQL serialises the database check instead.
        with patch('catalog.services.cache.add', side_effect=AssertionError):
            result = services.register_view_event(
                anime=self.anime, user=self.user, ip_address='1.1.1.1'
            )

        self.assertIsNotNone(result)
        self.assertTrue(ViewHistory.objects.exists())

    def test_view_event_falls_back_to_database_when_dedup_cache_is_unavailable(self):
        with patch('catalog.services.cache.get', side_effect=RuntimeError('cache down')):
            result = services.register_view_event(
                anime=self.anime, user=self.user, ip_address='1.1.1.1'
            )

        self.assertIsNotNone(result)
        self.assertEqual(ViewHistory.objects.count(), 1)

    def test_failed_insert_does_not_leave_a_24_hour_dedup_marker(self):
        dedup_key = services._view_dedup_key(self.anime, self.user, '1.1.1.1')

        with patch('catalog.services.ViewHistory.objects.create', side_effect=RuntimeError('db down')):
            with self.assertRaisesRegex(RuntimeError, 'db down'):
                services.register_view_event(
                    anime=self.anime, user=self.user, ip_address='1.1.1.1'
                )

        self.assertIsNone(cache.get(dedup_key))

    def test_postgresql_view_dedup_uses_a_transaction_scoped_lock(self):
        with patch('catalog.services.connection') as database:
            database.vendor = 'postgresql'
            cursor = database.cursor.return_value.__enter__.return_value

            services._acquire_view_dedup_lock(
                anime=self.anime, viewer=self.user, ip_address='1.1.1.1'
            )

        cursor.execute.assert_called_once()
        statement, parameters = cursor.execute.call_args.args
        self.assertEqual(statement, 'SELECT pg_advisory_xact_lock(%s)')
        self.assertEqual(len(parameters), 1)
        self.assertIsInstance(parameters[0], int)

    def test_existing_view_rebuilds_marker_only_for_remaining_window(self):
        view = ViewHistory.objects.create(anime=self.anime, user=self.user, ip_address='1.1.1.1')
        view.viewed_at = timezone.now() - services.VIEW_DEDUP_WINDOW + timedelta(seconds=10)
        view.save(update_fields=['viewed_at'])
        cache.clear()

        with patch('catalog.services.cache.set') as cache_set:
            with self.captureOnCommitCallbacks(execute=True):
                result = services.register_view_event(
                    anime=self.anime, user=self.user, ip_address='1.1.1.1'
                )

        self.assertIsNone(result)
        self.assertEqual(ViewHistory.objects.count(), 1)
        self.assertEqual(cache_set.call_count, 1)
        self.assertGreater(cache_set.call_args.args[2], 0)
        self.assertLessEqual(cache_set.call_args.args[2], 10)

    def test_rate_anime_updates_existing_rating(self):
        services.rate_anime(user=self.user, anime=self.anime, rating=3)
        avg = services.rate_anime(user=self.user, anime=self.anime, rating=5)

        self.assertEqual(self.anime.ratings.count(), 1)
        self.assertEqual(avg, 5.0)

    def test_rate_anime_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            services.rate_anime(user=self.user, anime=self.anime, rating=6)


class AggregateCacheTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='okabe', password='elpsykongroo')
        self.anime = AnimeDescription.objects.get(slug='steins-gate')

    def test_average_rating_is_cached(self):
        services.rate_anime(user=self.user, anime=self.anime, rating=4)
        self.assertEqual(services.average_rating(self.anime), 4.0)

        with self.assertNumQueries(0):
            self.assertEqual(services.average_rating(self.anime), 4.0)

    def test_rating_invalidates_average_cache(self):
        services.rate_anime(user=self.user, anime=self.anime, rating=2)
        self.assertEqual(services.average_rating(self.anime), 2.0)

        services.rate_anime(user=self.user, anime=self.anime, rating=5)

        self.assertEqual(services.average_rating(self.anime), 5.0)

    def test_total_views_is_cached(self):
        self.assertEqual(services.total_views(self.anime), 0)
        # TestCase itself wraps the test in a transaction; execute callbacks
        # explicitly to mirror the committed production request.
        with self.captureOnCommitCallbacks(execute=True):
            services.register_view_event(
                anime=self.anime, user=self.user, ip_address='1.1.1.1'
            )

        self.assertEqual(services.total_views(self.anime), 1)

        with self.assertNumQueries(0):
            services.total_views(self.anime)

    def test_anime_list_endpoint_is_cached(self):
        first = self.client.get('/api/anime')
        self.assertEqual(first.status_code, 200)

        with self.assertNumQueries(0):
            second = self.client.get('/api/anime')

        self.assertEqual(first.json(), second.json())


class ViewDedupCommitTest(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='okabe', password='elpsykongroo')
        self.anime, _ = AnimeDescription.objects.get_or_create(
            slug='steins-gate',
            defaults={
                'name': 'Steins;Gate',
                'appearing': '2011 весна',
                'type': 'TV',
                'genres': 'Sci-Fi',
                'description': 'Test title',
            },
        )

    def test_committed_view_sets_marker_and_releases_inflight_lock(self):
        dedup_key = services._view_dedup_key(self.anime, self.user, '1.1.1.1')

        with transaction.atomic():
            view = services.register_view_event(
                anime=self.anime, user=self.user, ip_address='1.1.1.1'
            )
            self.assertIsNotNone(view)
            self.assertIsNone(cache.get(dedup_key))

        self.assertIsNotNone(cache.get(dedup_key))


class CatalogApiTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_anime_list(self):
        response = self.client.get('/api/anime')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)
        self.assertIn('steins-gate', [item['slug'] for item in response.json()])

    def test_anime_stats_does_not_register_a_view(self):
        response = self.client.get('/api/anime/steins-gate')
        self.client.get('/api/anime/steins-gate')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['slug'], 'steins-gate')
        self.assertIsNone(data['avg_rating'])
        self.assertEqual(data['total_views'], 0)
        self.assertIsNone(data['user_rating'])
        self.assertEqual(ViewHistory.objects.count(), 0)

    def test_anime_stats_does_not_expose_static_metadata(self):
        data = self.client.get('/api/anime/steins-gate').json()

        self.assertEqual(
            set(data), {'slug', 'avg_rating', 'total_views', 'user_rating'}
        )

    def test_view_endpoint_registers_deduplicated_view(self):
        response = self.client.post('/api/anime/steins-gate/view', **csrf_headers(self.client))
        self.client.post('/api/anime/steins-gate/view', **csrf_headers(self.client))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(ViewHistory.objects.count(), 1)

    def test_view_endpoint_associates_authenticated_user(self):
        user = User.objects.create_user(username='okabe', password='elpsykongroo')
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.post('/api/anime/steins-gate/view', **csrf_headers(self.client))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(ViewHistory.objects.get().user, user)

    def test_view_endpoint_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post('/api/anime/steins-gate/view')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(ViewHistory.objects.count(), 0)

    def test_view_endpoint_unknown_slug_returns_404(self):
        response = self.client.post('/api/anime/unknown/view', **csrf_headers(self.client))

        self.assertEqual(response.status_code, 404)

    def test_unknown_slug_returns_404(self):
        response = self.client.get('/api/anime/unknown')

        self.assertEqual(response.status_code, 404)

    def test_rating_requires_auth(self):
        response = self.client.post(
            '/api/anime/steins-gate/rating', {'rating': 5}, content_type='application/json'
        )

        self.assertEqual(response.status_code, 401)

    def test_rating_flow(self):
        User.objects.create_user(username='okabe', password='elpsykongroo')
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.post(
            '/api/anime/steins-gate/rating',
            {'rating': 5},
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'avg_rating': 5.0, 'user_rating': 5})

    def test_rating_out_of_range_rejected(self):
        User.objects.create_user(username='okabe', password='elpsykongroo')
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.post(
            '/api/anime/steins-gate/rating',
            {'rating': 6},
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 422)
