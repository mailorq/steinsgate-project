import hmac
import math
import secrets
import uuid
from datetime import timedelta
from hashlib import sha256

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import salted_hmac


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    nickname = models.CharField(max_length=255, blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)

    def __str__(self):
        return self.user.username


def hash_verification_code(raw: str) -> str:
    return salted_hmac(
        "accounts.EmailVerificationCode", raw, secret=settings.SECRET_KEY, algorithm="sha256"
    ).hexdigest()


def email_delivery_fingerprint(email: str) -> str:
    return salted_hmac(
        "accounts.EmailDeliveryQuota",
        email.strip().lower(),
        secret=settings.EMAIL_DELIVERY_QUOTA_SECRET,
        algorithm="sha256",
    ).hexdigest()


class EmailVerificationCode(models.Model):
    MAX_ATTEMPTS = 5
    MAX_RESENDS = 5
    TTL = timedelta(minutes=15)
    RESEND_COOLDOWN = timedelta(seconds=60)
    RESEND_WINDOW = timedelta(hours=1)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='verification_code'
    )
    code_hash = models.CharField(max_length=64)
    code_nonce = models.CharField(max_length=64, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=0)
    resend_window_started_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    last_sent_at = models.DateTimeField(default=timezone.now)
    dispatch_token = models.UUIDField(null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_failed_at = models.DateTimeField(null=True, blank=True)

    DISPATCH_FIELDS = (
        "dispatch_token", "last_sent_at", "queued_at", "delivered_at", "delivery_failed_at",
    )

    def __str__(self):
        return f"{self.user.username} - {self.created_at}"

    def start_dispatch(self, now) -> None:
        self.dispatch_token = uuid.uuid4()
        self.last_sent_at = now
        self.queued_at = None
        self.delivered_at = None
        self.delivery_failed_at = None

    def rotate_code(self) -> str:
        self.code_nonce = secrets.token_urlsafe(32)
        raw = self.current_code()
        self.code_hash = hash_verification_code(raw)
        return raw

    def current_code(self) -> str:
        if not self.code_nonce:
            raise ValueError("Verification code nonce is missing")
        material = f"{self.user_id}:{self.code_nonce}".encode()
        digest = hmac.new(settings.SECRET_KEY.encode(), material, sha256).digest()
        return f"{int.from_bytes(digest[:8], byteorder='big') % 1_000_000:06d}"

    def matches(self, raw: str) -> bool:
        return bool(self.code_hash) and hmac.compare_digest(
            self.code_hash, hash_verification_code(raw)
        )

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.created_at + self.TTL

    @property
    def awaits_delivery(self) -> bool:
        return self.delivered_at is None and self.delivery_failed_at is None and not self.is_expired

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempts >= self.MAX_ATTEMPTS

    @property
    def resends_exhausted(self) -> bool:
        return self.resend_count >= self.MAX_RESENDS

    def resend_window_expired(self, now=None) -> bool:
        now = now or timezone.now()
        return now >= self.resend_window_started_at + self.RESEND_WINDOW

    def resend_window_remaining(self, now=None) -> int:
        now = now or timezone.now()
        remaining = self.resend_window_started_at + self.RESEND_WINDOW - now
        return max(math.ceil(remaining.total_seconds()), 0)

    @property
    def cooldown_remaining(self) -> int:
        remaining = self.RESEND_COOLDOWN - (timezone.now() - self.last_sent_at)
        return max(math.ceil(remaining.total_seconds()), 0)


class EmailDeliveryQuota(models.Model):
    MAX_DELIVERIES = EmailVerificationCode.MAX_RESENDS + 1
    WINDOW = timedelta(hours=1)

    email_fingerprint = models.CharField(max_length=64, unique=True)
    delivery_count = models.PositiveSmallIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["window_started_at"], name="accounts_edq_window_idx")]

    def __str__(self):
        return f"Email delivery quota #{self.pk}"

    def window_expired(self, now=None) -> bool:
        now = now or timezone.now()
        return now >= self.window_started_at + self.WINDOW

    def window_remaining(self, now=None) -> int:
        now = now or timezone.now()
        remaining = self.window_started_at + self.WINDOW - now
        return max(math.ceil(remaining.total_seconds()), 0)
