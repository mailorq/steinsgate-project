from celery import shared_task
from celery.utils.time import get_exponential_backoff_interval
from django.core.management import call_command

from . import services

EMAIL_MAX_RETRIES = 5
EMAIL_RETRY_BACKOFF = 15
EMAIL_RETRY_BACKOFF_MAX = 180

PURGE_BATCH_SIZE = 1_000
PURGE_MAX_BATCHES = 50


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES, rate_limit="30/m")
def send_verification_code(self, user_id: int, dispatch_token: str) -> None:
    try:
        services.deliver_verification_code(user_id=user_id, dispatch_token=dispatch_token)
    except services.TransientDeliveryError as error:
        if self.request.retries >= self.max_retries:
            services.record_delivery_failure(user_id=user_id, dispatch_token=dispatch_token)
            return
        countdown = get_exponential_backoff_interval(
            EMAIL_RETRY_BACKOFF, self.request.retries, EMAIL_RETRY_BACKOFF_MAX, full_jitter=True
        )
        raise self.retry(exc=error, countdown=countdown) from error


@shared_task
def reconcile_verification_delivery() -> None:
    services.reconcile_pending_deliveries()


@shared_task
def purge_expired_registrations() -> None:
    for _ in range(PURGE_MAX_BATCHES):
        result = services.purge_expired_registrations(batch_size=PURGE_BATCH_SIZE)
        if max(result.registrations, result.delivery_quotas) < PURGE_BATCH_SIZE:
            return


@shared_task
def clear_expired_sessions() -> None:
    call_command("clearsessions")
