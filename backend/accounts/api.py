from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import File, Router
from ninja.decorators import decorate_view
from ninja.files import UploadedFile
from ninja.security import django_auth
from ninja.utils import check_csrf

from config.network import get_client_ip
from config.throttling import (
    auth_throttles,
    register_throttles,
    resend_throttles,
    security_anon_throttles,
)

from . import lockout, services
from .schemas import (
    LoginIn,
    MessageOut,
    NicknameIn,
    RegisterIn,
    SessionOut,
    UserOut,
    VerificationDispatchOut,
    VerifyEmailIn,
)

auth_router = Router(tags=["auth"])
profile_router = Router(tags=["profile"])

AUTH_THROTTLES = security_anon_throttles(
    settings.API_AUTH_THROTTLE, settings.API_AUTH_THROTTLE_SUSTAINED
)
REGISTER_THROTTLES = AUTH_THROTTLES + register_throttles(settings.API_REGISTER_THROTTLE)
WRITE_THROTTLES = auth_throttles(settings.API_WRITE_THROTTLE, settings.API_WRITE_THROTTLE_SUSTAINED)
RESEND_THROTTLES = resend_throttles(settings.API_RESEND_THROTTLE)


def serialize_user(user: User) -> dict:
    profile = user.profile
    return {
        "username": user.username,
        "nickname": profile.nickname or user.username,
        "email": user.email,
        "avatar_url": profile.avatar.url if profile.avatar else None,
    }


def locked_response(error: lockout.LockedOut) -> tuple:
    return 429, {"detail": str(error)}


def resend_limited_response(
    error: (
        services.EmailDeliveryLimitError
        | services.SiteDeliveryLimitError
        | services.ResendCooldownError
        | services.ResendLimitError
    ),
):
    response = JsonResponse({"detail": str(error)}, status=429)
    response["Retry-After"] = str(error.retry_after)
    return response


# django-ninja снимает csrf проверку со всех view и возвращает ее только внутри cookie аутентификации, поэтому маршруты без auth проверяют токен сами
def csrf_rejected(request) -> tuple | None:
    if check_csrf(request) is not None:
        return 403, {"detail": "Проверка CSRF не пройдена"}
    return None


@auth_router.get("/csrf", response={204: None})
@decorate_view(ensure_csrf_cookie)
def csrf_token(request):
    return 204, None


@auth_router.get("/session", response=SessionOut)
def session(request):
    if request.user.is_authenticated:
        return {"user": serialize_user(request.user)}
    return {"user": None}


@auth_router.post(
    "/register",
    response={
        201: VerificationDispatchOut,
        400: MessageOut,
        403: MessageOut,
        429: MessageOut,
        503: MessageOut,
    },
    throttle=REGISTER_THROTTLES,
)
def register(request, payload: RegisterIn):
    if (rejected := csrf_rejected(request)) is not None:
        return rejected

    try:
        result = services.register_user(
            username=payload.username,
            email=payload.email,
            password=payload.password,
        )
    except services.RegistrationError as error:
        return 400, {"detail": str(error)}
    except (services.EmailDeliveryLimitError, services.SiteDeliveryLimitError) as error:
        return resend_limited_response(error)

    request.session["pending_user_id"] = result.user.id
    return 201, {
        "detail": "Код подтверждения отправлен на почту",
        "resend_available_in": result.resend_available_in,
    }


@auth_router.post(
    "/resend-verification",
    response={
        200: VerificationDispatchOut,
        400: MessageOut,
        403: MessageOut,
        429: MessageOut,
        503: MessageOut,
    },
    throttle=RESEND_THROTTLES,
)
def resend_verification(request):
    if (rejected := csrf_rejected(request)) is not None:
        return rejected

    pending_user_id = request.session.get("pending_user_id")
    user = User.objects.filter(id=pending_user_id, is_active=False).first()
    if user is None:
        return 400, {"detail": "Нет ожидающей подтверждения регистрации"}

    try:
        result = services.resend_verification(user=user)
    except (
        services.EmailDeliveryLimitError,
        services.SiteDeliveryLimitError,
        services.ResendCooldownError,
        services.ResendLimitError,
    ) as error:
        return resend_limited_response(error)
    except services.VerificationError as error:
        return 400, {"detail": str(error)}

    return 200, {
        "detail": "Код отправлен на почту",
        "resend_available_in": result.resend_available_in,
    }


@auth_router.post(
    "/verify-email",
    response={200: SessionOut, 400: MessageOut, 403: MessageOut, 429: MessageOut, 503: MessageOut},
    throttle=AUTH_THROTTLES,
)
def verify_email(request, payload: VerifyEmailIn):
    if (rejected := csrf_rejected(request)) is not None:
        return rejected

    ip = get_client_ip(request)
    try:
        lockout.check_blocked("verify", ip)
    except lockout.LockedOut as error:
        return locked_response(error)

    pending_user_id = request.session.get("pending_user_id")
    user = User.objects.filter(id=pending_user_id, is_active=False).first()
    if user is None:
        return 400, {"detail": "Нет ожидающей подтверждения регистрации"}

    try:
        user = services.verify_email(user=user, code=payload.code)
    except services.VerificationError as error:
        lockout.register_failure("verify", ip)
        return 400, {"detail": str(error)}

    lockout.reset("verify", ip)
    request.session.pop("pending_user_id", None)
    login(request, user)
    return 200, {"user": serialize_user(user)}


@auth_router.post(
    "/login",
    response={200: SessionOut, 400: MessageOut, 403: MessageOut, 429: MessageOut, 503: MessageOut},
    throttle=AUTH_THROTTLES,
)
def login_view(request, payload: LoginIn):
    if (rejected := csrf_rejected(request)) is not None:
        return rejected

    ip = get_client_ip(request)
    try:
        lockout.check_blocked("login", ip)
    except lockout.LockedOut as error:
        return locked_response(error)

    user = services.authenticate_user(
        request=request, username=payload.username, password=payload.password
    )
    if user is None:
        lockout.register_failure("login", ip)
        return 400, {"detail": "Неверное имя пользователя или пароль"}

    lockout.reset("login", ip)
    login(request, user)
    return 200, {"user": serialize_user(user)}


@auth_router.post("/logout", response={204: None}, auth=django_auth)
def logout_view(request):
    logout(request)
    return 204, None


@profile_router.patch(
    "",
    response={200: UserOut, 400: MessageOut},
    auth=django_auth,
    throttle=WRITE_THROTTLES,
)
def update_profile(request, payload: NicknameIn):
    try:
        services.update_nickname(user=request.user, nickname=payload.nickname)
    except services.ProfileError as error:
        return 400, {"detail": str(error)}
    return 200, serialize_user(request.user)


@profile_router.post(
    "/avatar",
    response={200: UserOut, 400: MessageOut},
    auth=django_auth,
    throttle=WRITE_THROTTLES,
)
def upload_avatar(request, avatar: File[UploadedFile]):
    try:
        services.update_avatar(user=request.user, avatar=avatar)
    except services.ProfileError as error:
        return 400, {"detail": str(error)}
    return 200, serialize_user(request.user)
