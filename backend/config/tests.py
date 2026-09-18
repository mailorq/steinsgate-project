import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase, override_settings
from ninja.conf import settings as ninja_settings

from . import celery as celery_app
from . import settings as project_settings
from .network import get_client_ip


class ClientIpTest(TestCase):
    """слева в x-forwarded-for стоит то, что прислал клиент, справа - nginx"""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, xff: str | None):
        headers = {"HTTP_X_FORWARDED_FOR": xff} if xff is not None else {}
        return self.factory.get("/", REMOTE_ADDR="10.0.0.1", **headers)

    def test_falls_back_to_remote_addr(self):
        self.assertEqual(get_client_ip(self._request(None)), "10.0.0.1")

    def test_spoofed_left_value_is_ignored(self):
        request = self._request("1.2.3.4, 203.0.113.7")
        self.assertEqual(get_client_ip(request), "203.0.113.7")

    def test_single_proxy_hop(self):
        self.assertEqual(get_client_ip(self._request("203.0.113.7")), "203.0.113.7")

    def test_ipv6_network_is_one_client(self):
        first = self.factory.get("/", REMOTE_ADDR="2001:db8:1:2:aaaa::1")
        second = self.factory.get("/", REMOTE_ADDR="2001:db8:1:2:ffff::9")
        other = self.factory.get("/", REMOTE_ADDR="2001:db8:1:3::1")

        self.assertEqual(get_client_ip(first), "2001:db8:1:2::")
        self.assertEqual(get_client_ip(first), get_client_ip(second))
        self.assertNotEqual(get_client_ip(first), get_client_ip(other))

    def test_ipv4_mapped_address_is_ipv4(self):
        self.assertEqual(get_client_ip(self._request("::ffff:203.0.113.7")), "203.0.113.7")

    def test_two_trusted_proxies(self):
        # NINJA_NUM_PROXIES читается ninja один раз при импорте,
        # override_settings до него не доходит.
        request = self._request("1.2.3.4, 203.0.113.7, 10.0.0.9")
        with patch.object(ninja_settings, "NUM_PROXIES", 2):
            self.assertEqual(get_client_ip(request), "203.0.113.7")


class StaffOnlyAdminTest(TestCase):

    PAGES = ('/admin/', '/admin/login/', '/admin/catalog/animedescription/', '/admin/missing/')

    def setUp(self):
        self.staff = User.objects.create_user(
            username='kurisu', password='makise_kurisu_1', is_staff=True, is_superuser=True
        )
        User.objects.create_user(username='daru', password='super_haker_1')

    def site_login(self, username, password):
        self.client.get('/api/auth/csrf')
        return self.client.post(
            '/api/auth/login',
            {'username': username, 'password': password},
            content_type='application/json',
            HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value,
        )

    def assert_hidden(self):
        for page in self.PAGES:
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], '/steins-gate')

    def test_anonymous_sees_nothing(self):
        self.assert_hidden()

    @override_settings(DEBUG=True)
    def test_debug_mode_does_not_change_the_redirect(self):
        response = self.client.get('/admin/')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/steins-gate')

    def test_admin_login_form_does_not_authenticate(self):
        self.client.get('/api/auth/csrf')
        response = self.client.post(
            '/admin/login/',
            {'username': 'kurisu', 'password': 'makise_kurisu_1',
             'csrfmiddlewaretoken': self.client.cookies['csrftoken'].value},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/steins-gate')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_regular_user_sees_nothing(self):
        self.assertEqual(self.site_login('daru', 'super_haker_1').status_code, 200)
        self.assert_hidden()

    def test_staff_enters_through_site_login(self):
        self.assertEqual(self.site_login('kurisu', 'makise_kurisu_1').status_code, 200)

        self.assertEqual(self.client.get('/admin/').status_code, 200)
        self.assertEqual(self.client.get('/admin/catalog/animedescription/').status_code, 200)


class WorkerHeartbeatTest(TestCase):

    def test_file_exists_only_while_the_consumer_runs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            celery_app, "HEARTBEAT_FILE", Path(directory) / "worker.heartbeat"
        ):
            timer = Mock()
            consumer = SimpleNamespace(timer=timer)
            step = celery_app.Heartbeat(consumer)

            step.start(consumer)
            self.assertTrue(celery_app.HEARTBEAT_FILE.exists())
            timer.call_repeatedly.assert_called_once()
            self.assertEqual(timer.call_repeatedly.call_args.args[0], celery_app.HEARTBEAT_INTERVAL)

            step.stop(consumer)
            timer.call_repeatedly.return_value.cancel.assert_called_once()
            self.assertFalse(celery_app.HEARTBEAT_FILE.exists())


@unittest.skipIf(os.name == "nt", "Unix file permissions are not portable on Windows")
class DevSecretKeyTest(TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary_directory.name)
        self.key_file = self.base_dir / ".dev-secret-key"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_creates_and_reuses_owner_only_secret(self):
        with patch.object(project_settings, "BASE_DIR", self.base_dir):
            first = project_settings._dev_secret_key()
            second = project_settings._dev_secret_key()

        self.assertEqual(first, second)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)

    def test_tightens_permissions_on_an_existing_secret(self):
        self.key_file.write_text("existing-secret", encoding="utf-8")
        os.chmod(self.key_file, 0o644)

        with patch.object(project_settings, "BASE_DIR", self.base_dir):
            self.assertEqual(project_settings._dev_secret_key(), "existing-secret")

        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)

    def test_refuses_a_symlink(self):
        target = self.base_dir / "target"
        target.write_text("secret", encoding="utf-8")
        try:
            self.key_file.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink is unavailable")

        with patch.object(project_settings, "BASE_DIR", self.base_dir):
            with self.assertRaises(ImproperlyConfigured):
                project_settings._dev_secret_key()
