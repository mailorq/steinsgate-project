"""
Django settings for the SteinsGate project.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/topics/settings/
"""

import os
import secrets
import stat
import sys
import time
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR.parent / ".env")

# Тестовый прогон не имеет .env и не должен требовать боевых секретов
# или редиректа на https.
TESTING = "test" in sys.argv

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _read_dev_secret_key(key_file: Path) -> str | None:
    flags = os.O_RDONLY
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_file, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ImproperlyConfigured(
            "Не удалось безопасно прочитать .dev-secret-key. "
            "Используйте явный SECRET_KEY в .env."
        ) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ImproperlyConfigured(".dev-secret-key должен быть обычным файлом")
        if os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        return os.read(descriptor, 1024).decode("utf-8").strip()
    except OSError as error:
        raise ImproperlyConfigured(
            "Не удалось безопасно прочитать .dev-secret-key. "
            "Используйте явный SECRET_KEY в .env."
        ) from error
    finally:
        os.close(descriptor)


def _create_dev_secret_key(key_file: Path) -> str:
    generated = secrets.token_urlsafe(64)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(key_file, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        remaining = generated.encode("utf-8")
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return generated


def _dev_secret_key() -> str:
    """Стабильный DEBUG-ключ с правами владельца и безопасным созданием.

    Константа в репозитории позволяла бы подделывать подписанные Django
    данные на любом стенде, где забыли .env, поэтому её здесь нет. на Unix
    файл создается/исправляется с mode 0600, чтобы другой пользователь хоста
    не смог читать ключ на demo-сервере
    """
    key_file = BASE_DIR / ".dev-secret-key"
    for _ in range(20):
        stored = _read_dev_secret_key(key_file)
        if stored:
            return stored
        if stored is None:
            try:
                return _create_dev_secret_key(key_file)
            except FileExistsError:
                # Another worker won O_EXCL and is writing the same key.
                pass
        time.sleep(0.01)
    raise ImproperlyConfigured(
        ".dev-secret-key существует, но не содержит ключа. "
        "Используйте явный SECRET_KEY в .env."
    )


# SECURITY WARNING: keep the secret key used in production secret.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if TESTING:
        SECRET_KEY = "secret-key-used-only-by-the-test-suite"
    elif DEBUG:
        SECRET_KEY = _dev_secret_key()
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY обязателен: задайте его в .env "
            "(python -c \"import secrets; print(secrets.token_urlsafe(64))\")"
        )

EMAIL_DELIVERY_QUOTA_SECRET = os.environ.get("EMAIL_DELIVERY_QUOTA_SECRET", "")
if not EMAIL_DELIVERY_QUOTA_SECRET:
    if TESTING:
        EMAIL_DELIVERY_QUOTA_SECRET = "email-delivery-quota-secret-for-tests-only"
    elif DEBUG:
        EMAIL_DELIVERY_QUOTA_SECRET = SECRET_KEY
    else:
        raise ImproperlyConfigured(
            "EMAIL_DELIVERY_QUOTA_SECRET обязателен при DEBUG=False: "
            "задайте отдельный стабильный секрет в .env"
        )

ALLOWED_HOSTS = _csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1")

# HTTPS-контур включается одним флагом: cookie с Secure, HSTS и редирект
# должны переключаться вместе, иначе DEBUG=False без TLS ломает вход —
# браузер просто не отправит Secure-cookie по http.
HTTPS_ENABLED = os.environ.get(
    "HTTPS_ENABLED", str(not DEBUG)
).lower() in ("true", "1", "yes")

# Cookies
SESSION_COOKIE_SECURE = HTTPS_ENABLED
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", 60 * 60 * 24 * 14))
CSRF_COOKIE_SECURE = HTTPS_ENABLED
CSRF_COOKIE_HTTPONLY = False

# CSRF
CSRF_TRUSTED_ORIGINS = _csv_env("CSRF_TRUSTED_ORIGINS")

# Vite dev-server проксирует /api с подменой Host, Origin браузера с ним не совпадает
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

# Заголовки
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
X_FRAME_OPTIONS = 'DENY'

# HTTPS (за nginx: протокол приходит в X-Forwarded-Proto)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = HTTPS_ENABLED and not TESTING
SECURE_HSTS_SECONDS = 31536000 if HTTPS_ENABLED else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS_ENABLED
SECURE_HSTS_PRELOAD = HTTPS_ENABLED

# Лимиты загрузки
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024   # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024     # 8 MB

# в контейнерах логи идут в stdout, ротацию выполняет драйвер логирования докер
LOG_TO_FILES = os.environ.get("LOG_TO_FILES", "True").lower() in ("true", "1", "yes")
LOG_DIR = BASE_DIR / "logs"
if LOG_TO_FILES:
    LOG_DIR.mkdir(exist_ok=True)


def _log_file(filename: str, level: str = "INFO") -> dict:
    return {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(LOG_DIR / filename),
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 3,
        "encoding": "utf-8",
        "formatter": "standard",
        "level": level,
    }


def _handlers(*names: str) -> list[str]:
    return [name for name in names if LOG_TO_FILES or not name.endswith("_file")]


_APP_LOGGER = {
    "handlers": _handlers("application_file", "console"),
    "level": "INFO",
    "propagate": False,
}

# access.log пишет только runserver, gunicorn выводит доступ в stdout
# application.log - доменные события, security.log - блокировки, CSRF и спам,
# error.log - ERROR и выше из всех логгеров
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,

    "formatters": {
        "standard": {
            "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        },
    },

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
        **(
            {
                "access_file": _log_file("access.log"),
                "application_file": _log_file("application.log"),
                "security_file": _log_file("security.log"),
                "error_file": _log_file("error.log", level="ERROR"),
            }
            if LOG_TO_FILES
            else {}
        ),
    },

    "loggers": {
        "django.server": {
            "handlers": _handlers("access_file", "console"),
            "level": "INFO",
            "propagate": False,
        },
        "django.security": {
            "handlers": _handlers("security_file", "console"),
            "level": "WARNING",
            "propagate": False,
        },
        "security": {
            "handlers": _handlers("security_file", "console"),
            "level": "INFO",
            "propagate": False,
        },
        "accounts": _APP_LOGGER,
        "catalog": _APP_LOGGER,
        "comments": _APP_LOGGER,
        "watch": _APP_LOGGER,
    },

    "root": {
        "handlers": _handlers("console", "error_file"),
        "level": "INFO",
    },
}


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'catalog',
    'comments',
    'watch',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]


MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# SMTP is intentional in DEBUG too: verification codes must reach the recipient.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    'django.core.mail.backends.smtp.EmailBackend',
)

EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))

EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')

EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "True").lower() in ("true", "1", "yes")
EMAIL_USE_TLS = not EMAIL_USE_SSL

DEFAULT_FROM_EMAIL = EMAIL_HOST_USER

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': os.getenv('DB_NAME', str(BASE_DIR / 'db.sqlite3')),
        'USER': os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', ''),
        'PORT': os.getenv('DB_PORT', ''),
    }
}
# Redis обслуживает троттлинг, IP-блокировки и кэш агрегатов; без него
# (локальная разработка) — память процесса
REDIS_URL = os.environ.get("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

# без брокера (локальный запуск без Docker) задачи выполняются синхронно в процессе
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "")
CELERY_TASK_ALWAYS_EAGER = not CELERY_BROKER_URL
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 500
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_TASK_TIME_LIMIT = 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# одна быстрая попытка публикации: запрос не ждёт недоступный брокер,
# неопубликованное письмо отправит сверка outbox
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BROKER_CONNECTION_TIMEOUT = 1
# visibility_timeout больше максимальной задержки ретрая и лимита времени задачи,
# иначе redis выдаст отложенную задачу второму процессу
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600, "socket_connect_timeout": 1}
CELERY_TASK_DEFAULT_QUEUE = "maintenance"
CELERY_TASK_ROUTES = {
    "accounts.tasks.send_verification_code": {"queue": "email"},
    "accounts.tasks.reconcile_verification_delivery": {"queue": "email"},
}
CELERY_BEAT_SCHEDULE = {
    "reconcile-verification-delivery": {
        "task": "accounts.tasks.reconcile_verification_delivery",
        "schedule": timedelta(minutes=1),
    },
    "purge-expired-registrations": {
        "task": "accounts.tasks.purge_expired_registrations",
        "schedule": timedelta(hours=1),
    },
    "purge-view-history": {
        "task": "catalog.tasks.purge_view_history",
        "schedule": timedelta(hours=1),
    },
    "clear-expired-sessions": {
        "task": "accounts.tasks.clear_expired_sessions",
        "schedule": timedelta(days=1),
    },
}

# Лимиты запросов к API: два окна на группу — всплеск и длинная дистанция
API_AUTH_THROTTLE = os.environ.get("API_AUTH_THROTTLE", "15/m")
API_AUTH_THROTTLE_SUSTAINED = os.environ.get("API_AUTH_THROTTLE_SUSTAINED", "100/h")
API_WRITE_THROTTLE = os.environ.get("API_WRITE_THROTTLE", "20/m")
API_WRITE_THROTTLE_SUSTAINED = os.environ.get("API_WRITE_THROTTLE_SUSTAINED", "300/h")
# Просмотр пишется отдельным POST, поэтому лимит не должен делить счетчик с оценками и комментариями.
API_VIEW_THROTTLE = os.environ.get("API_VIEW_THROTTLE", "30/m")
API_VIEW_THROTTLE_SUSTAINED = os.environ.get("API_VIEW_THROTTLE_SUSTAINED", "300/h")
# Плеер досылает прогресс каждые несколько секунд, поэтому у него свой лимит.
API_PROGRESS_THROTTLE = os.environ.get("API_PROGRESS_THROTTLE", "30/m")
API_PROGRESS_THROTTLE_SUSTAINED = os.environ.get("API_PROGRESS_THROTTLE_SUSTAINED", "600/h")
# Повторная отправка кода: строгий отдельный лимит на IP.
API_RESEND_THROTTLE = os.environ.get("API_RESEND_THROTTLE", "5/h")

# За nginx клиентский IP приходит в X-Forwarded-For; без этого троттлинг
# видит всех клиентов как один адрес прокси
NINJA_NUM_PROXIES = int(os.environ.get("NINJA_NUM_PROXIES", "1"))

# Схема API раскрывает карту эндпоинтов, поэтому по умолчанию она только в DEBUG.
API_DOCS_ENABLED = os.environ.get(
    "API_DOCS_ENABLED", str(DEBUG)
).lower() in ("true", "1", "yes")

if TESTING:
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    API_AUTH_THROTTLE = "10000/m"
    API_AUTH_THROTTLE_SUSTAINED = "10000/h"
    API_WRITE_THROTTLE = "10000/m"
    API_WRITE_THROTTLE_SUSTAINED = "10000/h"
    API_VIEW_THROTTLE = "10000/m"
    API_VIEW_THROTTLE_SUSTAINED = "10000/h"
    API_PROGRESS_THROTTLE = "10000/m"
    API_PROGRESS_THROTTLE_SUSTAINED = "10000/h"
    API_RESEND_THROTTLE = "10000/h"
    CELERY_BROKER_URL = "memory://"
    CELERY_TASK_ALWAYS_EAGER = True


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

# Статика фронта переехала в frontend/; STATIC_URL остаётся для админки
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
