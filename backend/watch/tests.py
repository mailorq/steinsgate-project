from contextlib import ExitStack
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache.backends.locmem import LocMemCache
from django.test import TestCase

from accounts.tests import csrf_headers

from .api import PROGRESS_THROTTLES

User = get_user_model()


class WatchApiTest(TestCase):

    URL = '/api/anime/steins-gate/progress'

    def setUp(self):
        self.user = User.objects.create_user(username='okabe', password='elpsykongroo')

    def test_anonymous_gets_401(self):
        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 401)

    def test_default_progress_is_zero(self):
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'current_time': 0.0, 'duration': 0.0, 'percentage': 0.0})

    def test_save_and_read_progress(self):
        self.client.login(username='okabe', password='elpsykongroo')
        headers = csrf_headers(self.client)

        response = self.client.put(
            self.URL,
            {'current_time': 600, 'duration': 1500},
            content_type='application/json',
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['percentage'], 40.0)

        saved = self.client.get(self.URL).json()
        self.assertEqual(saved['current_time'], 600.0)

    def test_progress_writes_are_throttled(self):
        self.client.login(username='okabe', password='elpsykongroo')
        headers = csrf_headers(self.client)
        payload = {'current_time': 600, 'duration': 1500}

        with ExitStack() as limits:
            for throttle in PROGRESS_THROTTLES:
                limits.enter_context(patch.object(throttle, 'num_requests', 1))
                limits.enter_context(
                    patch.object(throttle, 'cache', LocMemCache(f'progress-{id(self)}', {}))
                )
            first = self.client.put(
                self.URL, payload, content_type='application/json', **headers
            )
            second = self.client.put(
                self.URL, payload, content_type='application/json', **headers
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_non_finite_values_are_rejected(self):
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.put(
            self.URL,
            '{"current_time": Infinity, "duration": Infinity}',
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(b'Infinity', self.client.get(self.URL).content)

    def test_position_is_clamped_to_duration(self):
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.put(
            self.URL,
            {'current_time': 5000, 'duration': 1500},
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['current_time'], 1500.0)
        self.assertEqual(response.json()['percentage'], 100.0)

    def test_absurd_duration_is_rejected(self):
        self.client.login(username='okabe', password='elpsykongroo')

        response = self.client.put(
            self.URL,
            {'current_time': 0, 'duration': 10 ** 12},
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 422)
