import json
import secrets
import smtplib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import mail
from django.db import transaction
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from kombu.exceptions import OperationalError

from . import services, tasks
from .models import EmailVerificationCode
from .tests import csrf_headers

User = get_user_model()
PASSWORD = secrets.token_urlsafe(16)


def register(username="daru"):
    return services.register_user(
        username=username, email=f"{username}@gmail.com", password=PASSWORD
    ).user


def broker_down():
    return patch.object(
        tasks.send_verification_code, "apply_async", side_effect=OperationalError("broker down")
    )


class DeliveryOutboxTest(TransactionTestCase):

    def test_registration_is_published_after_commit_and_delivered(self):
        user = register()

        record = EmailVerificationCode.objects.get(user=user)
        self.assertIsNotNone(record.dispatch_token)
        self.assertIsNotNone(record.queued_at)
        self.assertIsNotNone(record.delivered_at)
        self.assertIsNone(record.delivery_failed_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(record.current_code(), mail.outbox[0].body)

    def test_task_receives_only_json_primitives(self):
        with patch.object(tasks.send_verification_code, "apply_async") as apply_async:
            user = register()

        args = apply_async.call_args.kwargs["args"]
        self.assertEqual(json.loads(json.dumps(args)), list(args))
        self.assertEqual(args, (user.pk, str(user.verification_code.dispatch_token)))

    def test_api_answers_201_when_broker_is_unavailable(self):
        client = Client()
        payload = {"username": "daru", "email": "daru@gmail.com", "password": PASSWORD}

        with broker_down():
            response = client.post(
                "/api/auth/register", payload, content_type="application/json",
                **csrf_headers(client),
            )

        self.assertEqual(response.status_code, 201)
        record = EmailVerificationCode.objects.get(user__username="daru")
        self.assertIsNone(record.queued_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_reconciliation_republishes_unqueued_dispatch(self):
        with broker_down():
            user = register()
        EmailVerificationCode.objects.filter(user=user).update(
            last_sent_at=timezone.now() - services.RECONCILE_GRACE - timedelta(seconds=1)
        )

        tasks.reconcile_verification_delivery.delay()

        record = EmailVerificationCode.objects.get(user=user)
        self.assertIsNotNone(record.queued_at)
        self.assertIsNotNone(record.delivered_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_reconciliation_ignores_recent_expired_queued_and_failed_dispatches(self):
        now = timezone.now()
        old = now - services.RECONCILE_GRACE - timedelta(seconds=1)
        with broker_down():
            recent, expired, queued, failed = (
                register(name) for name in ("recent", "expired", "queued", "failed")
            )
        codes = EmailVerificationCode.objects
        codes.filter(user=expired).update(
            last_sent_at=old, created_at=now - EmailVerificationCode.TTL - timedelta(seconds=1)
        )
        codes.filter(user=queued).update(last_sent_at=old, queued_at=now)
        codes.filter(user=failed).update(last_sent_at=old, delivery_failed_at=now)

        with patch.object(tasks.send_verification_code, "apply_async") as apply_async:
            republished = services.reconcile_pending_deliveries()

        self.assertEqual(republished, 0)
        apply_async.assert_not_called()


class DeferredPublishTest(TestCase):

    def test_publish_waits_for_the_outermost_commit(self):
        with patch.object(tasks.send_verification_code, "apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with transaction.atomic():
                    register()
                    apply_async.assert_not_called()

        self.assertEqual(len(callbacks), 1)
        apply_async.assert_called_once()

    def test_rolled_back_registration_publishes_nothing(self):
        with patch.object(tasks.send_verification_code, "apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        register()
                        raise RuntimeError("rollback")

        self.assertEqual(callbacks, [])
        apply_async.assert_not_called()


class SendVerificationTaskTest(TransactionTestCase):

    def setUp(self):
        with patch.object(tasks.send_verification_code, "apply_async"):
            self.user = register()
        self.token = str(self.user.verification_code.dispatch_token)

    def run_task(self, **send_mail_patch):
        with patch("accounts.services.send_mail", **send_mail_patch) as send_mail:
            tasks.send_verification_code.apply(args=(self.user.pk, self.token))
        return send_mail, EmailVerificationCode.objects.get(user=self.user)

    def test_transient_errors_are_retried_until_failure_is_recorded(self):
        send_mail, record = self.run_task(side_effect=smtplib.SMTPServerDisconnected("closed"))

        self.assertEqual(send_mail.call_count, tasks.EMAIL_MAX_RETRIES + 1)
        self.assertIsNotNone(record.delivery_failed_at)
        self.assertIsNone(record.delivered_at)

    def test_temporary_smtp_replies_recover_on_retry(self):
        temporary = (
            TimeoutError("timed out"),
            ConnectionRefusedError(),
            smtplib.SMTPResponseException(451, b"try again later"),
            smtplib.SMTPRecipientsRefused({"daru@gmail.com": (450, b"mailbox busy")}),
        )
        for error in temporary:
            with self.subTest(error=type(error).__name__):
                EmailVerificationCode.objects.filter(user=self.user).update(delivered_at=None)
                send_mail, record = self.run_task(side_effect=[error, 1])
                self.assertEqual(send_mail.call_count, 2)
                self.assertIsNotNone(record.delivered_at)

    def test_permanent_errors_are_recorded_without_retry(self):
        permanent = (
            smtplib.SMTPAuthenticationError(535, b"bad credentials"),
            smtplib.SMTPRecipientsRefused({"daru@gmail.com": (550, b"no such user")}),
            smtplib.SMTPDataError(554, b"rejected"),
            smtplib.SMTPNotSupportedError("AUTH not supported"),
        )
        for error in permanent:
            with self.subTest(error=type(error).__name__):
                EmailVerificationCode.objects.filter(user=self.user).update(delivery_failed_at=None)
                send_mail, record = self.run_task(side_effect=error)
                self.assertEqual(send_mail.call_count, 1)
                self.assertIsNotNone(record.delivery_failed_at)

    def test_backend_accepting_nothing_is_a_failure(self):
        send_mail, record = self.run_task(return_value=0)

        self.assertEqual(send_mail.call_count, 1)
        self.assertIsNotNone(record.delivery_failed_at)

    def assert_skipped(self, **record_changes):
        EmailVerificationCode.objects.filter(user=self.user).update(**record_changes)
        send_mail, _ = self.run_task(return_value=1)
        send_mail.assert_not_called()

    def test_superseded_dispatch_is_skipped(self):
        self.assert_skipped(dispatch_token=uuid.uuid4())

    def test_delivered_dispatch_is_not_sent_twice(self):
        self.assert_skipped(delivered_at=timezone.now())

    def test_expired_code_is_not_sent(self):
        self.assert_skipped(
            created_at=timezone.now() - EmailVerificationCode.TTL - timedelta(seconds=1)
        )

    def test_activated_account_is_not_sent_a_code(self):
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.assert_skipped()


class MaintenanceTaskTest(TestCase):

    def test_expired_sessions_are_cleared(self):
        expired, active = SessionStore(), SessionStore()
        expired.create()
        active.create()
        Session.objects.filter(session_key=expired.session_key).update(
            expire_date=timezone.now() - timedelta(days=1)
        )

        tasks.clear_expired_sessions.delay()

        self.assertEqual(
            list(Session.objects.values_list("session_key", flat=True)), [active.session_key]
        )

    def test_purge_drains_backlog_in_batches(self):
        with patch.object(tasks.send_verification_code, "apply_async"):
            users = [register(f"pending{index}") for index in range(5)]
        EmailVerificationCode.objects.filter(user__in=users).update(
            created_at=timezone.now() - EmailVerificationCode.TTL - timedelta(minutes=1)
        )

        with patch.object(tasks, "PURGE_BATCH_SIZE", 2):
            tasks.purge_expired_registrations.delay()

        self.assertFalse(User.objects.filter(username__startswith="pending").exists())
