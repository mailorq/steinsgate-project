import io
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from smtplib import SMTPServerDisconnected
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from config.throttling import SecurityAnonBurstThrottle

from . import lockout, services
from .models import EmailDeliveryQuota, EmailVerificationCode, email_delivery_fingerprint

User = get_user_model()


def csrf_headers(client):
    client.get("/api/auth/csrf")
    return {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}


def code_from_email():
    import re
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)


class RegistrationServiceTest(TransactionTestCase):

    def test_email_domain_validation(self):
        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='test', email='test@blocked.com', password='complex_pass_123'
            )

    def test_duplicate_email_rejected(self):
        User.objects.create_user(username='taken', email='taken@gmail.com', password='x')

        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='newuser', email='taken@gmail.com', password='complex_pass_123'
            )

    def test_duplicate_username_rejected(self):
        User.objects.create_user(username='taken', email='one@gmail.com', password='x')

        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='taken', email='two@gmail.com', password='complex_pass_123'
            )

    def test_weak_password_rejected(self):
        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='test', email='test@gmail.com', password='12345678'
            )

    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {
                'NAME': (
                    'django.contrib.auth.password_validation.'
                    'UserAttributeSimilarityValidator'
                ),
            },
        ]
    )
    def test_password_similar_to_username_is_rejected(self):
        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='kurisu', email='kurisu@gmail.com', password='kurisu123'
            )

    def test_email_is_normalized_before_persistence(self):
        result = services.register_user(
            username='test', email='  TEST@GMAIL.COM  ', password='complex_pass_123'
        )

        self.assertEqual(result.user.email, 'test@gmail.com')

    def test_recipient_delivery_quota_resets_only_after_its_window(self):
        email = 'test@gmail.com'
        quota = EmailDeliveryQuota.objects.create(
            email_fingerprint=email_delivery_fingerprint(email),
            delivery_count=EmailDeliveryQuota.MAX_DELIVERIES,
            window_started_at=timezone.now() - EmailDeliveryQuota.WINDOW - timedelta(seconds=1),
        )

        services.register_user(
            username='test', email=email, password='complex_pass_123'
        )

        quota.refresh_from_db()
        self.assertEqual(quota.delivery_count, 1)

    def test_recipient_delivery_quota_survives_secret_key_rotation(self):
        email = 'test@gmail.com'
        quota = EmailDeliveryQuota.objects.create(
            email_fingerprint=email_delivery_fingerprint(email),
            delivery_count=EmailDeliveryQuota.MAX_DELIVERIES,
        )

        with override_settings(SECRET_KEY='rotated-django-secret-for-test-only'):
            with self.assertRaises(services.EmailDeliveryLimitError):
                services.register_user(
                    username='test', email=email, password='complex_pass_123'
                )

        self.assertEqual(EmailDeliveryQuota.objects.count(), 1)
        quota.refresh_from_db()
        self.assertEqual(quota.delivery_count, EmailDeliveryQuota.MAX_DELIVERIES)


class VerificationServiceTest(TransactionTestCase):

    def setUp(self):
        self.user = services.register_user(
            username='kurisu',
            email='kurisu@gmail.com',
            password='complex_pass_123',
        ).user
        self.code = code_from_email()

    def test_registered_user_is_inactive_with_code(self):
        self.assertFalse(self.user.is_active)
        self.assertEqual(len(self.user.verification_code.code_hash), 64)

    def test_raw_code_is_not_persisted(self):
        record = self.user.verification_code

        self.assertNotEqual(record.code_hash, self.code)
        self.assertNotEqual(record.code_nonce, self.code)
        self.assertFalse(hasattr(record, 'code'))

    def test_correct_code_activates_user(self):
        services.verify_email(user=self.user, code=self.code)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(EmailVerificationCode.objects.filter(user=self.user).exists())

    def test_wrong_code_raises_and_keeps_user_inactive(self):
        with self.assertRaises(services.VerificationError):
            services.verify_email(user=self.user, code='000000')

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_attempts_are_limited(self):
        for _ in range(EmailVerificationCode.MAX_ATTEMPTS):
            with self.assertRaises(services.VerificationError):
                services.verify_email(user=self.user, code='000000')

        with self.assertRaises(services.VerificationError):
            services.verify_email(user=self.user, code=self.code)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.verification_code.attempts, EmailVerificationCode.MAX_ATTEMPTS)

    def test_expired_code_rejected(self):
        record = self.user.verification_code
        record.created_at = timezone.now() - EmailVerificationCode.TTL - timedelta(minutes=1)
        record.save(update_fields=['created_at'])

        with self.assertRaises(services.VerificationError):
            services.verify_email(user=self.user, code=self.code)

    def test_inactive_user_cannot_login(self):
        logged_in = self.client.login(username='kurisu', password='complex_pass_123')

        self.assertFalse(logged_in)


class LockoutTest(TransactionTestCase):

    def setUp(self):
        cache.clear()
        User.objects.create_user(username='okabe', password='correct_horse_1')

    def login(self, password, address='127.0.0.1'):
        return self.client.post(
            '/api/auth/login',
            {'username': 'okabe', 'password': password},
            content_type='application/json',
            REMOTE_ADDR=address,
        )

    def test_rotating_ipv6_inside_one_network_shares_the_block(self):
        for host in range(1, 6):
            self.assertEqual(self.login('wrong', f'2001:db8:1:2::{host}').status_code, 400)

        self.assertEqual(self.login('wrong', '2001:db8:1:2:ffff::1').status_code, 429)
        self.assertEqual(self.login('wrong', '2001:db8:1:3::1').status_code, 400)

    def test_soft_block_after_five_failures(self):
        for _ in range(5):
            self.assertEqual(self.login('wrong').status_code, 400)

        blocked = self.login('wrong')
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Повторите через', blocked.json()['detail'])

        also_blocked_with_correct = self.login('correct_horse_1')
        self.assertEqual(also_blocked_with_correct.status_code, 429)

    def test_success_resets_counter(self):
        for _ in range(4):
            self.login('wrong')

        self.assertEqual(self.login('correct_horse_1').status_code, 200)

        for _ in range(5):
            self.assertEqual(self.login('wrong').status_code, 400)

    def test_hard_block_after_twenty_failures(self):
        for _ in range(lockout.HARD_LIMIT):
            lockout.register_failure('login', '1.2.3.4')

        with self.assertRaises(lockout.LockedOut) as caught:
            lockout.check_blocked('login', '1.2.3.4')

        self.assertGreater(caught.exception.retry_after, lockout.SOFT_BLOCK_SECONDS)

    def test_five_fresh_attempts_after_block_expires(self):
        for _ in range(5):
            lockout.register_failure('login', '5.6.7.8')

        with self.assertRaises(lockout.LockedOut):
            lockout.check_blocked('login', '5.6.7.8')

        cache.delete('lockout:login:5.6.7.8:block')

        for _ in range(4):
            lockout.register_failure('login', '5.6.7.8')
        lockout.check_blocked('login', '5.6.7.8')

        lockout.register_failure('login', '5.6.7.8')
        with self.assertRaises(lockout.LockedOut):
            lockout.check_blocked('login', '5.6.7.8')

    def test_verify_email_lockout(self):
        response = self.client.post(
            '/api/auth/register',
            {'username': 'kurisu', 'email': 'kurisu@gmail.com', 'password': 'complex_pass_123'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)

        for _ in range(5):
            wrong = self.client.post(
                '/api/auth/verify-email', {'code': '000000'}, content_type='application/json'
            )
            self.assertEqual(wrong.status_code, 400)

        blocked = self.client.post(
            '/api/auth/verify-email', {'code': '000000'}, content_type='application/json'
        )
        self.assertEqual(blocked.status_code, 429)


class AuthApiTest(TransactionTestCase):

    def setUp(self):
        cache.clear()

    REGISTER_PAYLOAD = {
        'username': 'kurisu',
        'email': 'kurisu@gmail.com',
        'password': 'complex_pass_123',
    }

    def register(self):
        return self.client.post(
            '/api/auth/register', self.REGISTER_PAYLOAD, content_type='application/json'
        )

    def test_register_creates_inactive_user_and_sends_email(self):
        response = self.register()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {'detail', 'resend_available_in'})
        self.assertGreater(response.json()['resend_available_in'], 0)
        user = User.objects.get(username='kurisu')
        self.assertFalse(user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('verification', mail.outbox[0].subject.lower())
        self.assertIsNotNone(user.verification_code.delivered_at)

    def test_register_returns_retry_after_when_recipient_quota_is_exhausted(self):
        EmailDeliveryQuota.objects.create(
            email_fingerprint=email_delivery_fingerprint(self.REGISTER_PAYLOAD['email']),
            delivery_count=EmailDeliveryQuota.MAX_DELIVERIES,
        )

        response = self.register()

        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response['Retry-After']), 0)
        self.assertFalse(User.objects.filter(username='kurisu').exists())

    def test_register_rejects_bad_domain(self):
        response = self.client.post(
            '/api/auth/register',
            {**self.REGISTER_PAYLOAD, 'email': 'kurisu@blocked.com'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username='kurisu').exists())

    def test_full_registration_flow(self):
        self.register()
        user = User.objects.get(username='kurisu')

        wrong = self.client.post(
            '/api/auth/verify-email', {'code': '000000'}, content_type='application/json'
        )
        self.assertEqual(wrong.status_code, 400)

        response = self.client.post(
            '/api/auth/verify-email',
            {'code': code_from_email()},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['username'], 'kurisu')
        self.assertTrue(response.wsgi_request.user.is_active)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertEqual(user.profile.nickname, 'kurisu')

    def test_verify_without_pending_registration(self):
        response = self.client.post(
            '/api/auth/verify-email', {'code': '123456'}, content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)

    def test_resend_reuses_still_valid_code_without_creating_another_user(self):
        self.register()
        user = User.objects.get(username='kurisu')
        record = user.verification_code
        old_hash = record.code_hash
        old_code = code_from_email()
        record.last_sent_at = timezone.now() - EmailVerificationCode.RESEND_COOLDOWN
        record.save(update_fields=['last_sent_at'])

        response = self.client.post('/api/auth/resend-verification')

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.code_hash, old_hash)
        self.assertEqual(code_from_email(), old_code)
        self.assertEqual(record.resend_count, 1)
        self.assertEqual(User.objects.filter(username='kurisu').count(), 1)

    def test_resend_cooldown_returns_retry_after(self):
        self.register()

        response = self.client.post('/api/auth/resend-verification')

        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response['Retry-After']), 0)

    def test_resend_returns_retry_after_when_recipient_quota_is_exhausted(self):
        self.register()
        user = User.objects.get(username='kurisu')
        record = user.verification_code
        record.last_sent_at = timezone.now() - EmailVerificationCode.RESEND_COOLDOWN
        record.save(update_fields=['last_sent_at'])
        quota = EmailDeliveryQuota.objects.get(
            email_fingerprint=email_delivery_fingerprint(user.email)
        )
        quota.delivery_count = EmailDeliveryQuota.MAX_DELIVERIES
        quota.save(update_fields=['delivery_count'])

        response = self.client.post('/api/auth/resend-verification')

        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response['Retry-After']), 0)

    def test_resend_without_pending_registration_is_rejected(self):
        response = self.client.post('/api/auth/resend-verification')

        self.assertEqual(response.status_code, 400)

    def test_login_and_session(self):
        User.objects.create_user(username='daru', password='super_haker_123')

        anonymous = self.client.get('/api/auth/session')
        self.assertIsNone(anonymous.json()['user'])

        bad = self.client.post(
            '/api/auth/login',
            {'username': 'daru', 'password': 'wrong'},
            content_type='application/json',
        )
        self.assertEqual(bad.status_code, 400)

        response = self.client.post(
            '/api/auth/login',
            {'username': 'daru', 'password': 'super_haker_123'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['username'], 'daru')

        session = self.client.get('/api/auth/session')
        self.assertEqual(session.json()['user']['username'], 'daru')

    def test_logout(self):
        User.objects.create_user(username='daru', password='super_haker_123')
        self.client.login(username='daru', password='super_haker_123')

        response = self.client.post('/api/auth/logout', **csrf_headers(self.client))

        self.assertEqual(response.status_code, 204)
        self.assertIsNone(self.client.get('/api/auth/session').json()['user'])


class ProfileApiTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='okabe', password='elpsykongroo')
        self.client.login(username='okabe', password='elpsykongroo')

    def test_update_nickname(self):
        response = self.client.patch(
            '/api/profile',
            {'nickname': 'Hououin Kyouma'},
            content_type='application/json',
            **csrf_headers(self.client),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['nickname'], 'Hououin Kyouma')
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.nickname, 'Hououin Kyouma')

    def test_anonymous_cannot_update_profile(self):
        self.client.logout()

        response = self.client.patch(
            '/api/profile', {'nickname': 'x'}, content_type='application/json'
        )

        self.assertEqual(response.status_code, 401)


class CsrfEnforcementTest(TransactionTestCase):
    """django-ninja снимает CSRF со всех view, маршруты без auth проверяют сами"""

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.payload = {
            'username': 'okabe',
            'email': 'okabe@gmail.com',
            'password': 'complex_pass_123',
        }

    def test_register_without_token_rejected(self):
        response = self.client.post(
            '/api/auth/register', self.payload, content_type='application/json'
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username='okabe').exists())

    def test_login_without_token_rejected(self):
        response = self.client.post(
            '/api/auth/login',
            {'username': 'okabe', 'password': 'complex_pass_123'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_verify_without_token_rejected(self):
        response = self.client.post(
            '/api/auth/verify-email', {'code': '123456'}, content_type='application/json'
        )
        self.assertEqual(response.status_code, 403)

    def test_resend_without_token_rejected(self):
        user = User.objects.create_user(
            username='pending', email='pending@gmail.com', password='complex_pass_123', is_active=False
        )
        record = EmailVerificationCode(user=user)
        record.rotate_code()
        record.save()
        session = self.client.session
        session['pending_user_id'] = user.pk
        session.save()

        response = self.client.post('/api/auth/resend-verification')

        self.assertEqual(response.status_code, 403)

    def test_register_with_token_accepted(self):
        response = self.client.post(
            '/api/auth/register',
            self.payload,
            content_type='application/json',
            **csrf_headers(self.client),
        )
        self.assertEqual(response.status_code, 201)


class EmailDeliveryTransactionTest(TransactionTestCase):
    """TransactionTestCase не оборачивает тест во внешнюю atomic-транзакцию."""

    def test_send_happens_outside_database_transaction(self):
        seen = {}

        def spy(*args, **kwargs):
            seen['in_atomic_block'] = connection.in_atomic_block
            return 1

        with patch('accounts.services.send_mail', side_effect=spy):
            services.register_user(
                username='daru', email='daru@gmail.com', password='complex_pass_123'
            )

        self.assertFalse(seen['in_atomic_block'])


class FailingCache:
    def get(self, *args, **kwargs):
        raise RuntimeError('cache unavailable')


class SlowCache(LocMemCache):
    """кеш проекта живет в redis: вызов уходит по сети и отпускает GIL"""

    def _over_network(self):
        time.sleep(0.002)

    def get(self, *args, **kwargs):
        self._over_network()
        return super().get(*args, **kwargs)

    def set(self, *args, **kwargs):
        self._over_network()
        return super().set(*args, **kwargs)

    def add(self, *args, **kwargs):
        self._over_network()
        return super().add(*args, **kwargs)

    def incr(self, *args, **kwargs):
        self._over_network()
        return super().incr(*args, **kwargs)


class SecurityThrottleTest(TransactionTestCase):
    def test_security_throttle_keeps_retry_after_for_a_normal_limit(self):
        request = RequestFactory().post('/api/auth/register')
        throttle = SecurityAnonBurstThrottle('1/m')
        # Do not share the project's throttle cache: earlier API tests may
        # legitimately have consumed the loopback address budget.
        throttle.cache = LocMemCache(f'security-throttle-{id(self)}', {})

        self.assertTrue(throttle.allow_request(request))
        self.assertFalse(throttle.allow_request(request))
        self.assertGreater(throttle.wait(), 0)

    def test_parallel_clients_do_not_share_throttle_state(self):
        # gunicorn обслуживает ручку несколькими потоками через один объект троттла
        throttle = SecurityAnonBurstThrottle('10/m')
        throttle.cache = SlowCache(f'throttle-threads-{id(self)}', {})
        factory = RequestFactory()
        verdicts = []
        guard = threading.Lock()

        def hammer(ip):
            for _ in range(10):
                allowed = throttle.allow_request(factory.post('/api/auth/register', REMOTE_ADDR=ip))
                with guard:
                    verdicts.append(allowed)

        threads = [threading.Thread(target=hammer, args=(f'10.0.0.{i}',)) for i in range(1, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(verdicts, [True] * 40)
        self.assertFalse(
            throttle.allow_request(factory.post('/api/auth/register', REMOTE_ADDR='10.0.0.1'))
        )

    def test_ipv6_rotation_inside_one_network_shares_a_counter(self):
        throttle = SecurityAnonBurstThrottle('1/m')
        throttle.cache = LocMemCache(f'throttle-ipv6-{id(self)}', {})
        factory = RequestFactory()

        def allowed(address):
            return throttle.allow_request(factory.post('/api/auth/login', REMOTE_ADDR=address))

        self.assertTrue(allowed('2001:db8:1:2::1'))
        self.assertFalse(allowed('2001:db8:1:2::2'))
        self.assertTrue(allowed('2001:db8:1:3::1'))

    def test_security_throttle_fails_closed(self):
        request = RequestFactory().post('/api/auth/register')
        throttle = SecurityAnonBurstThrottle('1/m')
        throttle.cache = FailingCache()

        self.assertFalse(throttle.allow_request(request))
        self.assertTrue(request._security_throttle_unavailable)

    def test_registration_returns_503_when_throttle_storage_is_unavailable(self):
        client = Client()
        with patch.object(SecurityAnonBurstThrottle, 'cache', FailingCache()):
            response = client.post(
                '/api/auth/register',
                {'username': 'daru', 'email': 'daru@gmail.com', 'password': 'complex_pass_123'},
                content_type='application/json',
                **csrf_headers(client),
            )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.filter(username='daru').exists())


class ResendVerificationServiceTest(TransactionTestCase):

    def setUp(self):
        with patch('accounts.models.secrets.token_urlsafe', return_value='initial-nonce'):
            self.user = services.register_user(
                username='mayuri', email='mayuri@gmail.com', password='complex_pass_123'
            ).user
        self.initial_code = code_from_email()

    def make_resendable(self):
        record = self.user.verification_code
        record.last_sent_at = timezone.now() - EmailVerificationCode.RESEND_COOLDOWN
        record.save(update_fields=['last_sent_at'])
        return record

    def assert_delivered(self):
        record = EmailVerificationCode.objects.get(user=self.user)
        self.assertIsNotNone(record.delivered_at)
        return record

    def test_resend_reuses_current_code_when_the_previous_delivery_may_have_succeeded(self):
        self.make_resendable()

        first_token = self.user.verification_code.dispatch_token

        services.resend_verification(user=self.user)

        self.assertNotEqual(self.assert_delivered().dispatch_token, first_token)
        self.assertEqual(code_from_email(), self.initial_code)
        services.verify_email(user=self.user, code=self.initial_code)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_resend_consumes_the_same_recipient_delivery_budget(self):
        self.make_resendable()

        services.resend_verification(user=self.user)

        quota = EmailDeliveryQuota.objects.get(
            email_fingerprint=email_delivery_fingerprint(self.user.email)
        )
        self.assertEqual(quota.delivery_count, 2)

    def test_expired_code_is_replaced(self):
        record = self.make_resendable()
        old_hash = record.code_hash
        record.created_at = timezone.now() - EmailVerificationCode.TTL - timedelta(seconds=1)
        record.save(update_fields=['created_at'])

        with patch('accounts.models.secrets.token_urlsafe', return_value='rotated-nonce'):
            services.resend_verification(user=self.user)

        self.assert_delivered()
        new_code = code_from_email()
        record.refresh_from_db()
        self.assertNotEqual(record.code_hash, old_hash)
        self.assertNotEqual(new_code, self.initial_code)
        with self.assertRaises(services.VerificationError):
            services.verify_email(user=self.user, code=self.initial_code)
        services.verify_email(user=self.user, code=new_code)

    def test_unsuccessful_resend_keeps_the_previous_code_valid(self):
        record = self.make_resendable()
        old_hash = record.code_hash

        with patch('accounts.services.send_mail', side_effect=SMTPServerDisconnected('closed')):
            services.resend_verification(user=self.user)

        record.refresh_from_db()
        self.assertIsNotNone(record.delivery_failed_at)
        self.assertEqual(record.code_hash, old_hash)
        services.verify_email(user=self.user, code=self.initial_code)

    def test_resend_respects_cooldown(self):
        with self.assertRaises(services.ResendCooldownError) as caught:
            services.resend_verification(user=self.user)

        self.assertGreater(caught.exception.retry_after, 0)

    def test_resend_respects_limit_until_the_current_code_expires(self):
        record = self.make_resendable()
        record.resend_count = EmailVerificationCode.MAX_RESENDS
        record.save(update_fields=['resend_count'])

        with self.assertRaises(services.ResendLimitError) as caught:
            services.resend_verification(user=self.user)

        self.assertGreater(caught.exception.retry_after, 0)

    def test_attempt_limit_does_not_reset_the_hourly_resend_limit(self):
        record = self.make_resendable()
        record.attempts = EmailVerificationCode.MAX_ATTEMPTS
        record.resend_count = EmailVerificationCode.MAX_RESENDS
        record.save(update_fields=['attempts', 'resend_count'])

        with self.assertRaises(services.ResendLimitError):
            services.resend_verification(user=self.user)

    def test_resend_limit_resets_only_after_its_window(self):
        record = self.make_resendable()
        record.resend_count = EmailVerificationCode.MAX_RESENDS
        record.resend_window_started_at = (
            timezone.now() - EmailVerificationCode.RESEND_WINDOW - timedelta(seconds=1)
        )
        record.save(update_fields=['resend_count', 'resend_window_started_at'])

        services.resend_verification(user=self.user)

        record = self.assert_delivered()
        self.assertEqual(record.resend_count, 1)

    @override_settings(SECRET_KEY='rotated-verification-key-for-test-only')
    def test_resend_rotates_code_after_secret_key_change(self):
        self.make_resendable()

        services.resend_verification(user=self.user)

        self.assert_delivered()
        new_code = code_from_email()
        self.assertNotEqual(new_code, self.initial_code)
        services.verify_email(user=self.user, code=new_code)

    def test_legacy_record_without_nonce_can_be_resent_immediately(self):
        record = self.user.verification_code
        record.code_hash = ''
        record.code_nonce = ''
        record.last_sent_at = timezone.now()
        record.save(update_fields=['code_hash', 'code_nonce', 'last_sent_at'])

        services.resend_verification(user=self.user)

        record = self.assert_delivered()
        self.assertTrue(record.code_nonce)
        self.assertTrue(record.matches(code_from_email()))


class StaleRegistrationTest(TestCase):

    def test_expired_unverified_registration_frees_email(self):
        first = services.register_user(
            username='squatter', email='victim@gmail.com', password='complex_pass_123'
        ).user
        record = EmailVerificationCode.objects.get(user=first)
        record.created_at = timezone.now() - EmailVerificationCode.TTL - timedelta(minutes=1)
        record.save(update_fields=['created_at'])

        second = services.register_user(
            username='victim', email='victim@gmail.com', password='complex_pass_123'
        ).user

        self.assertNotEqual(first.pk, second.pk)
        self.assertFalse(User.objects.filter(pk=first.pk).exists())

    def test_re_registration_cannot_reset_recipient_delivery_quota(self):
        first = services.register_user(
            username='squatter', email='victim@gmail.com', password='complex_pass_123'
        ).user
        record = EmailVerificationCode.objects.get(user=first)
        record.created_at = timezone.now() - EmailVerificationCode.TTL - timedelta(seconds=1)
        record.save(update_fields=['created_at'])
        quota = EmailDeliveryQuota.objects.get(
            email_fingerprint=email_delivery_fingerprint('victim@gmail.com')
        )
        quota.delivery_count = EmailDeliveryQuota.MAX_DELIVERIES
        quota.save(update_fields=['delivery_count'])

        with self.assertRaises(services.EmailDeliveryLimitError):
            services.register_user(
                username='victim', email='victim@gmail.com', password='complex_pass_123'
            )

        self.assertTrue(User.objects.filter(pk=first.pk).exists())
        self.assertEqual(User.objects.filter(email='victim@gmail.com').count(), 1)

    def test_disabled_account_is_not_purged(self):
        banned = User.objects.create_user(
            username='banned', email='banned@gmail.com', password='x', is_active=False
        )

        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='banned', email='banned@gmail.com', password='complex_pass_123'
            )

        self.assertTrue(User.objects.filter(pk=banned.pk).exists())

    def test_pending_registration_still_blocks(self):
        services.register_user(
            username='squatter', email='victim@gmail.com', password='complex_pass_123'
        )

        with self.assertRaises(services.RegistrationError):
            services.register_user(
                username='victim', email='victim@gmail.com', password='complex_pass_123'
            )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='sg-avatar-test-'))
class AvatarValidationTest(TestCase):

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings['MEDIA_ROOT'], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(
            username='mayuri', email='mayuri@gmail.com', password='complex_pass_123'
        )

    @staticmethod
    def _png(color: str = 'red') -> SimpleUploadedFile:
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
        return SimpleUploadedFile('avatar.png', buffer.getvalue(), content_type='image/png')

    def test_disguised_file_rejected(self):
        payload = SimpleUploadedFile(
            'avatar.png', b'<html><script>alert(1)</script></html>', content_type='image/png'
        )

        with self.assertRaises(services.ProfileError):
            services.update_avatar(user=self.user, avatar=payload)

        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.avatar)

    def test_real_image_accepted(self):
        services.update_avatar(user=self.user, avatar=self._png())

        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.avatar)

    def test_excessive_resolution_rejected(self):
        buffer = io.BytesIO()
        Image.new('1', (4_001, 4_001)).save(buffer, format='PNG')
        avatar = SimpleUploadedFile('avatar.png', buffer.getvalue(), content_type='image/png')

        with self.assertRaises(services.ProfileError):
            services.update_avatar(user=self.user, avatar=avatar)

    def test_previous_file_removed_on_replace(self):
        services.update_avatar(user=self.user, avatar=self._png('red'))
        profile = self.user.profile
        profile.refresh_from_db()
        first_path = profile.avatar.path
        storage = profile.avatar.storage

        services.update_avatar(user=self.user, avatar=self._png('blue'))
        profile.refresh_from_db()

        self.assertNotEqual(profile.avatar.path, first_path)
        self.assertFalse(storage.exists(first_path))
