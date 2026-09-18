import logging

from ninja.throttling import AnonRateThrottle, AuthRateThrottle, UserRateThrottle

from .network import get_client_ip

logger = logging.getLogger(__name__)


class AtomicWindowMixin:
    """
    ninja держит ключ и историю запросов на самом объекте троттла, а объект один на все запросы ручки:
    под gthread соседние потоки перетирают состояние друг друга и запрос уходит в чужой счетчик
    здесь на объекте состояния нет, счетчик окна лежит в кеше и растет атомарно
    """

    def allow_request(self, request) -> bool:
        key = self.get_cache_key(request)
        if key is None:
            return True

        window = f"{key}:{int(self.timer() // self.duration)}"
        if self.cache.add(window, 1, self.duration):
            return True
        try:
            count = self.cache.incr(window)
        except ValueError:
            # окно истекло между add и incr, запрос попадает в следующее
            return True
        if count == 1:
            # ключ истек уже внутри incr, и redis пересоздал его без срока жизни
            self.cache.touch(window, self.duration)
        return count <= self.num_requests

    def wait(self) -> float:
        return self.duration - (self.timer() % self.duration)

    # клиент тот же, что у lockout: свой разбор X-Forwarded-For в ninja не сводит IPv6 к /64
    def get_ident(self, request):
        return get_client_ip(request)


class FailOpenMixin:
    """недоступное хранилище счетчиков не должно ронять api"""

    def allow_request(self, request):
        try:
            return super().allow_request(request)
        except Exception:
            logger.exception("Throttle storage unavailable, request allowed")
            return True


class FailClosedMixin:
    def allow_request(self, request):
        try:
            return super().allow_request(request)
        except Exception:
            logger.exception("Security throttle storage unavailable, request denied")
            request._security_throttle_unavailable = True
            return False


# у каждого окна свой scope инстансы с общим scope делят счетчик в кеше,
# и второй уровень лимита считал бы те же запросы


class AuthBurstThrottle(FailOpenMixin, AtomicWindowMixin, AuthRateThrottle):
    scope = "auth_burst"


class AuthSustainedThrottle(FailOpenMixin, AtomicWindowMixin, AuthRateThrottle):
    scope = "auth_sustained"


class SecurityAnonBurstThrottle(FailClosedMixin, AtomicWindowMixin, AnonRateThrottle):
    scope = "security_anon_burst"


class SecurityAnonSustainedThrottle(FailClosedMixin, AtomicWindowMixin, AnonRateThrottle):
    scope = "security_anon_sustained"


class SecurityAnonResendThrottle(FailClosedMixin, AtomicWindowMixin, AnonRateThrottle):
    scope = "anon_resend"


class SecurityAnonRegisterThrottle(FailClosedMixin, AtomicWindowMixin, AnonRateThrottle):
    scope = "anon_register"


class ViewEventBurstThrottle(FailOpenMixin, AtomicWindowMixin, UserRateThrottle):
    scope = "view_event_burst"


class ViewEventSustainedThrottle(FailOpenMixin, AtomicWindowMixin, UserRateThrottle):
    scope = "view_event_sustained"


class ProgressBurstThrottle(FailOpenMixin, AtomicWindowMixin, UserRateThrottle):
    scope = "progress_burst"


class ProgressSustainedThrottle(FailOpenMixin, AtomicWindowMixin, UserRateThrottle):
    scope = "progress_sustained"


def security_anon_throttles(burst_rate: str, sustained_rate: str) -> list:
    return [
        SecurityAnonBurstThrottle(burst_rate),
        SecurityAnonSustainedThrottle(sustained_rate),
    ]


def resend_throttles(rate: str) -> list:
    return [SecurityAnonResendThrottle(rate)]


def register_throttles(rate: str) -> list:
    return [SecurityAnonRegisterThrottle(rate)]


def view_event_throttles(burst_rate: str, sustained_rate: str) -> list:
    return [ViewEventBurstThrottle(burst_rate), ViewEventSustainedThrottle(sustained_rate)]


def progress_throttles(burst_rate: str, sustained_rate: str) -> list:
    return [ProgressBurstThrottle(burst_rate), ProgressSustainedThrottle(sustained_rate)]


def auth_throttles(burst_rate: str, sustained_rate: str) -> list:
    return [AuthBurstThrottle(burst_rate), AuthSustainedThrottle(sustained_rate)]
