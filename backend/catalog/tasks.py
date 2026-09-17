from celery import shared_task

from . import services

PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 100


@shared_task
def purge_view_history() -> None:
    for _ in range(PURGE_MAX_BATCHES):
        if services.purge_view_history(batch_size=PURGE_BATCH_SIZE) < PURGE_BATCH_SIZE:
            return
