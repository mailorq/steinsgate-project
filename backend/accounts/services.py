import logging
import os
import smtplib
import warnings
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerUnavailableError
from PIL import Image

from .models import EmailDeliveryQuota, EmailVerificationCode, email_delivery_fingerprint

logger = logging.getLogger(__name__)

ALLOWED_EMAIL_DOMAINS = (
    "@gmail.com", "@yahoo.com", "@ukr.net", "@mail.ru",
    "@yandex.ru", "@outlook.com", "@icloud.com",
)
MAX_NICKNAME_LENGTH = 50
ALLOWED_AVATAR_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
ALLOWED_AVATAR_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")
MAX_AVATAR_SIZE = 8 * 1024 * 1024
MAX_AVATAR_PIXELS = 16_000_000
RECONCILE_GRACE = timedelta(minutes=1)
RECONCILE_BATCH_SIZE = 500
# общий счетчик сайта в таблице квот: отпечаток адреса это hex HMAC и с ним не совпадет
SITE_DELIVERY_FINGERPRINT = "site"


class RegistrationError(Exception):
    pass


class VerificationError(Exception):
    pass


class TransientDeliveryError(Exception):
    pass


class ResendCooldownError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Повторная отправка возможна через {retry_after} сек")


class ResendLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(
            "Лимит повторных отправок исчерпан. "
            f"Запросите новый код через {retry_after} сек"
        )


class EmailDeliveryLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(
            "Для этого адреса достигнут лимит отправки кодов. "
            f"Попробуйте через {retry_after} сек"
        )


class SiteDeliveryLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(
            "Отправка кодов временно приостановлена. "
            f"Попробуйте через {retry_after} сек"
        )


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class VerificationDispatch:
    user: User
    resend_available_in: int


@dataclass(frozen=True)
class PurgeResult:
    registrations: int
    delivery_quotas: int


def _issue_code(record: EmailVerificationCode, now) -> None:
    record.rotate_code()
    record.attempts = 0
    record.created_at = now
    record.start_dispatch(now)


def _lock_or_create_email_delivery_quota(fingerprint: str) -> EmailDeliveryQuota:
    """Returns a row locked for this transaction, including first-use races."""
    while True:
        try:
            return EmailDeliveryQuota.objects.select_for_update().get(
                email_fingerprint=fingerprint
            )
        except EmailDeliveryQuota.DoesNotExist:
            try:
                with transaction.atomic():
                    return EmailDeliveryQuota.objects.create(email_fingerprint=fingerprint)
            except IntegrityError:
                continue


def _claim_delivery_quota(fingerprint: str, limit: int) -> int:
    """Засчитывает письмо в окне под блокировкой строки. 0 разрешает отправку, иначе возвращает секунды до нового окна"""
    quota = _lock_or_create_email_delivery_quota(fingerprint)
    now = timezone.now()

    if quota.window_expired(now):
        quota.delivery_count = 0
        quota.window_started_at = now
    if quota.delivery_count >= limit:
        return max(quota.window_remaining(now), 1)

    quota.delivery_count += 1
    quota.save(update_fields=["delivery_count", "window_started_at"])
    return 0


def _claim_email_delivery_quota(email: str) -> None:
    fingerprint = email_delivery_fingerprint(email)
    if retry_after := _claim_delivery_quota(fingerprint, EmailDeliveryQuota.MAX_DELIVERIES):
        raise EmailDeliveryLimitError(retry_after)
    limit = settings.EMAIL_DELIVERY_HOURLY_LIMIT
    if retry_after := _claim_delivery_quota(SITE_DELIVERY_FINGERPRINT, limit):
        raise SiteDeliveryLimitError(retry_after)


def _is_transient_smtp_error(error: Exception) -> bool:
    # SMTPException наследует OSError, поэтому ответы сервера разбираются раньше
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return all(400 <= code < 500 for code, _ in error.recipients.values())
    if isinstance(error, smtplib.SMTPResponseException):
        return 400 <= error.smtp_code < 500
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(error, smtplib.SMTPException):
        return False
    return isinstance(error, OSError)


def _mark_dispatch(user_id: int, dispatch_token, **fields) -> None:
    EmailVerificationCode.objects.filter(
        user_id=user_id, dispatch_token=dispatch_token
    ).update(**fields)


def _schedule_delivery(record: EmailVerificationCode) -> None:
    user_id, dispatch_token = record.user_id, record.dispatch_token
    transaction.on_commit(
        lambda: publish_delivery(user_id=user_id, dispatch_token=dispatch_token),
        robust=True,
    )


def publish_delivery(*, user_id: int, dispatch_token) -> None:
    from .tasks import send_verification_code

    try:
        send_verification_code.apply_async(args=(user_id, str(dispatch_token)))
    except BrokerUnavailableError:
        logger.warning("Verification email not queued: broker unavailable")
        return
    _mark_dispatch(user_id, dispatch_token, queued_at=timezone.now())


def reconcile_pending_deliveries() -> int:
    now = timezone.now()
    pending = list(
        EmailVerificationCode.objects.filter(
            dispatch_token__isnull=False,
            queued_at__isnull=True,
            delivered_at__isnull=True,
            delivery_failed_at__isnull=True,
            last_sent_at__lt=now - RECONCILE_GRACE,
            created_at__gt=now - EmailVerificationCode.TTL,
            user__is_active=False,
        ).values_list("user_id", "dispatch_token")[:RECONCILE_BATCH_SIZE]
    )
    for user_id, dispatch_token in pending:
        publish_delivery(user_id=user_id, dispatch_token=dispatch_token)
    if pending:
        logger.info("Unqueued verification emails republished", extra={"count": len(pending)})
    return len(pending)


def deliver_verification_code(*, user_id: int, dispatch_token: str) -> None:
    record = (
        EmailVerificationCode.objects.select_related("user")
        .filter(user_id=user_id, dispatch_token=dispatch_token, user__is_active=False)
        .first()
    )
    if record is None or not record.awaits_delivery:
        return

    code = record.current_code()
    try:
        sent = send_mail(
            subject="Verification Email",
            message=f"Your verification code is: {code}",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[record.user.email],
        )
    except Exception as error:
        # ответ сервера и трассировка несут адрес получателя, в лог идет только класс
        if _is_transient_smtp_error(error):
            raise TransientDeliveryError(type(error).__name__) from error
        logger.error(f"Verification email rejected by the mail server: {type(error).__name__}")
        sent = 0
    else:
        if sent != 1:
            logger.warning("Verification email not accepted by the mail backend")

    if sent == 1:
        _mark_dispatch(user_id, dispatch_token, delivered_at=timezone.now())
    else:
        _mark_dispatch(user_id, dispatch_token, delivery_failed_at=timezone.now())


def record_delivery_failure(*, user_id: int, dispatch_token: str) -> None:
    logger.error("Verification email not delivered after all retries")
    _mark_dispatch(user_id, dispatch_token, delivery_failed_at=timezone.now())


def _consume_code(record, code: str, not_found_message: str) -> str | None:
    if record is None:
        return not_found_message
    if record.is_expired:
        return "Код истек. Запросите новый."
    if record.attempts_exhausted:
        return "Попытки исчерпаны. Запросите новый код."

    record.attempts += 1
    record.save(update_fields=["attempts"])

    if not record.matches(code):
        remaining = record.MAX_ATTEMPTS - record.attempts
        return f"Неверный код. Осталось попыток: {remaining}"

    record.delete()
    return None


def _purge_stale_registration(*, username: str, email: str) -> None:
    candidates = User.objects.filter(is_active=False).filter(
        Q(username=username) | Q(email__iexact=email)
    )
    for user in candidates:
        record = EmailVerificationCode.objects.filter(user=user).first()
        if record is not None and record.is_expired:
            user.delete()
            logger.info("Stale registration purged")


def purge_expired_registrations(*, batch_size: int = 1_000, dry_run: bool = False) -> PurgeResult:
    now = timezone.now()
    cutoff = now - EmailVerificationCode.TTL
    quota_cutoff = now - EmailDeliveryQuota.WINDOW
    with transaction.atomic():
        user_ids = list(
            User.objects.select_for_update()
            .filter(is_active=False, verification_code__created_at__lt=cutoff)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        # регистрация держит строку адреса и строку сайта, пакет в порядке id встал бы к ним в обратном порядке: занятые строки дочистит следующий запуск
        quota_ids = list(
            EmailDeliveryQuota.objects.select_for_update(skip_locked=True)
            .filter(window_started_at__lt=quota_cutoff)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if user_ids and not dry_run:
            User.objects.filter(pk__in=user_ids, is_active=False).delete()
        if quota_ids and not dry_run:
            EmailDeliveryQuota.objects.filter(pk__in=quota_ids).delete()

    if (user_ids or quota_ids) and not dry_run:
        logger.info(
            "Expired verification data purged",
            extra={"registrations": len(user_ids), "delivery_quotas": len(quota_ids)},
        )
    return PurgeResult(registrations=len(user_ids), delivery_quotas=len(quota_ids))


def _validate_registration(*, username: str, email: str, password: str) -> None:
    try:
        UnicodeUsernameValidator()(username)
        validate_email(email)
    except DjangoValidationError as error:
        raise RegistrationError("; ".join(error.messages)) from None

    if not email.endswith(ALLOWED_EMAIL_DOMAINS):
        raise RegistrationError(
            "Допустимые домены почты: " + ", ".join(ALLOWED_EMAIL_DOMAINS)
        )
    if User.objects.filter(username=username).exists():
        raise RegistrationError("Имя пользователя уже занято")
    if User.objects.filter(email=email).exists():
        raise RegistrationError("Email уже используется")

    try:
        validate_password(password, user=User(username=username, email=email))
    except DjangoValidationError as error:
        raise RegistrationError("; ".join(error.messages)) from None


def register_user(*, username: str, email: str, password: str) -> VerificationDispatch:
    username = username.strip()
    email = email.strip().lower()

    with transaction.atomic():
        _purge_stale_registration(username=username, email=email)
        _validate_registration(username=username, email=email, password=password)

        try:
            user = User.objects.create_user(
                username=username, email=email, password=password, is_active=False
            )
        except IntegrityError:
            raise RegistrationError("Имя пользователя или email уже используется") from None

        record = EmailVerificationCode(user=user)
        _issue_code(record, timezone.now())
        record.save()
        _claim_email_delivery_quota(email)
        _schedule_delivery(record)

    logger.info("Registration created")
    return VerificationDispatch(user=user, resend_available_in=record.cooldown_remaining)


def resend_verification(*, user: User) -> VerificationDispatch:
    # select_for_update сериализует параллельные resend'ы: cooldown и лимит
    # повторов нельзя обойти гонкой.
    with transaction.atomic():
        pending_user = (
            User.objects.select_for_update().filter(pk=user.pk, is_active=False).first()
        )
        if pending_user is None:
            raise VerificationError("Нет ожидающей подтверждения регистрации")
        record = (
            EmailVerificationCode.objects.select_for_update().filter(user=pending_user).first()
        )
        if record is None:
            raise VerificationError("Нет ожидающей подтверждения регистрации")
        now = timezone.now()
        if record.resend_window_expired(now):
            record.resend_count = 0
            record.resend_window_started_at = now

        code_matches_current_secret = (
            bool(record.code_nonce) and record.matches(record.current_code())
        )
        needs_new_code = (
            record.is_expired
            or record.attempts_exhausted
            or not code_matches_current_secret
        )
        if record.resends_exhausted:
            raise ResendLimitError(max(record.resend_window_remaining(now), 1))
        if not needs_new_code and record.cooldown_remaining > 0:
            raise ResendCooldownError(record.cooldown_remaining)

        if needs_new_code:
            _issue_code(record, now)
        else:
            record.start_dispatch(now)
        record.resend_count += 1
        record.save(
            update_fields=[
                "code_hash", "code_nonce", "attempts", "created_at",
                "resend_count", "resend_window_started_at",
                *EmailVerificationCode.DISPATCH_FIELDS,
            ]
        )
        _claim_email_delivery_quota(pending_user.email)
        _schedule_delivery(record)

    return VerificationDispatch(user=pending_user, resend_available_in=record.cooldown_remaining)


def verify_email(*, user: User, code: str) -> User:
    with transaction.atomic():
        pending_user = (
            User.objects.select_for_update().filter(pk=user.pk, is_active=False).first()
        )
        record = (
            EmailVerificationCode.objects.select_for_update().filter(user=pending_user).first()
            if pending_user is not None
            else None
        )
        error = _consume_code(record, code, "Код не найден. Пройдите регистрацию заново.")
        if error is None:
            pending_user.is_active = True
            pending_user.save(update_fields=["is_active"])
    # Исключение бросаем после коммита: инкремент попытки должен сохраниться.
    if error is not None:
        raise VerificationError(error)
    return pending_user


def authenticate_user(*, request, username: str, password: str) -> User | None:
    return authenticate(request, username=username, password=password)


def update_nickname(*, user: User, nickname: str) -> None:
    cleaned = nickname.strip()
    if not cleaned or len(cleaned) > MAX_NICKNAME_LENGTH:
        raise ProfileError(f"Никнейм должен быть от 1 до {MAX_NICKNAME_LENGTH} символов")

    user.profile.nickname = cleaned
    user.profile.save(update_fields=["nickname"])
    logger.debug("Nickname changed")


def update_avatar(*, user: User, avatar) -> None:
    if avatar.size > MAX_AVATAR_SIZE:
        raise ProfileError("Файл слишком большой. Максимум 8 МБ")

    extension = os.path.splitext(avatar.name)[1].lower()
    if extension not in ALLOWED_AVATAR_EXTENSIONS:
        raise ProfileError("Допустимые форматы: JPG, PNG, GIF, WEBP")

    # ``formats`` не даёт Pillow даже разбирать неподдерживаемые форматы: одно
    # расширение можно подделать. лимит пикселей защищает worker от сжатых
    # изображений с чрезмерными размерами
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(avatar, formats=ALLOWED_AVATAR_FORMATS) as image:
                if image.width * image.height > MAX_AVATAR_PIXELS:
                    raise ProfileError("Изображение слишком большое по разрешению")
                image.verify()
    except ProfileError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ProfileError("Изображение слишком большое по разрешению") from None
    except Exception:
        raise ProfileError("Файл не является изображением") from None
    finally:
        avatar.seek(0)

    profile = user.profile
    previous = profile.avatar.name
    profile.avatar = avatar
    profile.save(update_fields=["avatar"])

    # ImageField не удаляет прежний файл
    if previous and previous != profile.avatar.name:
        try:
            profile.avatar.storage.delete(previous)
        except OSError:
            logger.warning(f"Old avatar not removed, path={previous}")

    logger.debug("Avatar changed")
