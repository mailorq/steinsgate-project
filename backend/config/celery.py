import os
import tempfile
from pathlib import Path

from celery import Celery, bootsteps

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "celery-worker.heartbeat"
HEARTBEAT_INTERVAL = 15


class Heartbeat(bootsteps.StartStopStep):
    """обновляет файл пока воркер подключен к брокеру и забирает задачи"""

    requires = {"celery.worker.consumer.tasks:Tasks"}

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.timer_ref = None

    def start(self, parent):
        HEARTBEAT_FILE.touch()
        self.timer_ref = parent.timer.call_repeatedly(HEARTBEAT_INTERVAL, HEARTBEAT_FILE.touch)

    def stop(self, parent):
        if self.timer_ref is not None:
            self.timer_ref.cancel()
            self.timer_ref = None
        HEARTBEAT_FILE.unlink(missing_ok=True)


app.steps["consumer"].add(Heartbeat)
