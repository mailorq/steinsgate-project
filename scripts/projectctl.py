#!/usr/bin/env python3
"""
подготовка и запуск проекта steins gate через docker compose
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_NAME = "steinsgate_mailor"
COMPOSE_FILE = "compose.yaml"
DEMO_OVERRIDE = "compose.demo.yaml"
ENV_FILE = ".env"
STATE_FILE = ".projectctl-state.json"
HEALTH_PATH = "/api/anime"
ROOT = Path(__file__).resolve().parent.parent

MIN_SECRET_LENGTH = 50
INTERPOLATED_SECRETS = (
    "SECRET_KEY",
    "EMAIL_DELIVERY_QUOTA_SECRET",
    "DB_PASSWORD",
    "EMAIL_HOST_PASSWORD",
)
OPTIONAL_SECRETS = ("EMAIL_HOST_PASSWORD",)
PUBLISHING_SERVICES = {"frontend"}
BACKGROUND_SERVICES = {"celery-worker": "healthy", "celery-beat": "running"}
FAILED_SERVICE_STATES = {"exited", "dead", "restarting", "unhealthy"}
DEFAULT_PORT = 4173
HEALTH_TIMEOUT = 180
DOCKER_TIMEOUT = 120
BUILD_TIMEOUT = 1800

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_WORKDIR_LABEL = "com.docker.compose.project.working_dir"

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


class CtlError(Exception):
    pass


def info(message: str) -> None:
    print(f"==> {message}")


def warn(message: str) -> None:
    print(f"[!] {message}")


def ok(message: str) -> None:
    print(f"[ok] {message}")


# значения окружения


def env_path() -> Path:
    return ROOT / ENV_FILE


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def is_truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def generate_secret() -> str:
    return secrets.token_urlsafe(64)


# безопасная запись .env


def harden_env_permissions(path: Path) -> bool:
    if os.name == "nt":
        return False
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return True


def _atomic_write(path: Path, content: str, *, secure: bool) -> None:
    """запись через временный файл без symlink подмены и без частичного файл"""
    if path.is_symlink():
        raise CtlError(
            f"{path.name} является символической ссылкой. "
            "Удалите ссылку и повторите — запись по ссылке небезопасна."
        )
    if path.exists() and not path.is_file():
        raise CtlError(f"{path.name} существует и не является обычным файлом")

    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        if secure and os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_env_atomic(path: Path, content: str) -> None:
    _atomic_write(path, content, secure=True)
    harden_env_permissions(path)


def state_path() -> Path:
    return ROOT / STATE_FILE


def read_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(resources: dict) -> None:
    payload = {
        "project": PROJECT_NAME,
        "workspace": str(ROOT),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resources": resources,
    }
    _atomic_write(state_path(), json.dumps(payload, indent=2), secure=False)


def workspace_owns_project() -> bool:
    state = read_state()
    return state.get("project") == PROJECT_NAME and _same_path(
        str(state.get("workspace", "")), str(ROOT)
    )


def set_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise CtlError(f"Значение {key} содержит перевод строки")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    write_env_atomic(path, "\n".join(lines) + "\n")


# валидация


def validate_interpolation_safe(key: str, value: str) -> list[str]:
    """значение попадает в docker compose, поэтому проверяем его представление"""
    problems = []
    if "$" in value:
        problems.append(f"{key} содержит '$' — docker compose исказит значение")
    if value[:1] in "\"'" or value[-1:] in "\"'":
        problems.append(f"{key} взят в кавычки — они попадут в значение")
    if "\n" in value or "\r" in value:
        problems.append(f"{key} содержит перевод строки")
    if value != value.strip():
        problems.append(f"{key} содержит пробелы по краям")
    elif any(character.isspace() for character in value):
        if key in OPTIONAL_SECRETS:
            return problems
        problems.append(f"{key} содержит пробельные символы")
    return problems


def validate_required_secret(key: str, value: str) -> list[str]:
    if not value:
        return [f"{key} пуст. Сгенерируйте URL-safe секрет длиной не менее {MIN_SECRET_LENGTH}"]
    problems = []
    if len(value) < MIN_SECRET_LENGTH:
        problems.append(f"{key} короче {MIN_SECRET_LENGTH} символов")
    problems.extend(validate_interpolation_safe(key, value))
    return problems


def validate_secret(value: str) -> list[str]:
    return validate_required_secret("SECRET_KEY", value)


def validate_host(value: str) -> tuple[str, list[str]]:
    """разрешен только hostname или ip: без схемы, порта, путей и служебных символов"""
    if not value:
        return "", ["Домен не задан"]
    if any(character in value for character in "\r\n\0"):
        return "", ["Домен содержит управляющие символы"]
    if any(character.isspace() for character in value):
        return "", ["Домен содержит пробелы"]
    if "=" in value:
        return "", ["Домен содержит '=' — попытка подстановки переменной"]
    if "://" in value or "/" in value:
        return "", ["Домен указывается без схемы и пути: steins.example.com"]
    if "$" in value or "`" in value:
        return "", ["Домен содержит символы подстановки"]

    candidate = value.strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
        return (f"[{address}]" if address.version == 6 else str(address)), []
    except ValueError:
        pass

    if ":" in value:
        return "", ["Домен указывается без порта"]
    if not HOSTNAME_RE.match(value):
        return "", ["Домен не является корректным hostname"]
    return value, []


def validate_port(raw: str) -> tuple[int | None, list[str]]:
    if not raw:
        return None, ["APP_PORT не задан"]
    try:
        port = int(raw)
    except ValueError:
        return None, ["APP_PORT должен быть числом"]
    if not 1 <= port <= 65535:
        return None, ["APP_PORT вне диапазона 1-65535"]
    return port, []


def resolve_mode(env: dict[str, str]) -> str:
    raw = env.get("PROJECTCTL_MODE", "").strip()
    if not raw:
        return "demo" if is_truthy(env.get("DEBUG", "False")) else "production"
    mode = raw.lower()
    if mode not in ("demo", "production"):
        raise CtlError(
            f"PROJECTCTL_MODE={raw!r} не распознан. Допустимо: demo или production"
        )
    return mode


def validate_env_values(env: dict[str, str], *, allow_debug: bool) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    problems.extend(validate_secret(env.get("SECRET_KEY", "")))
    problems.extend(
        validate_required_secret(
            "EMAIL_DELIVERY_QUOTA_SECRET",
            env.get("EMAIL_DELIVERY_QUOTA_SECRET", ""),
        )
    )

    for key in INTERPOLATED_SECRETS:
        value = env.get(key, "")
        if key in {"SECRET_KEY", "EMAIL_DELIVERY_QUOTA_SECRET"}:
            continue
        if not value:
            if key not in OPTIONAL_SECRETS:
                problems.append(f"{key} пуст")
            continue
        problems.extend(validate_interpolation_safe(key, value))

    port, port_problems = validate_port(env.get("APP_PORT", ""))
    problems.extend(port_problems)

    mode = resolve_mode(env)
    debug = is_truthy(env.get("DEBUG", "False"))
    if debug and mode == "production":
        problems.append(
            "DEBUG=True недопустим в режиме production: TLS-прокси делает сервис публичным. "
            "Отладку запускайте только в режиме demo"
        )
    elif debug and not allow_debug:
        problems.append(
            "DEBUG=True. Это раскрывает настройки и трассировки. "
            "Для осознанного запуска повторите команду с --allow-debug"
        )
    elif debug:
        warnings.append("DEBUG=True подтверждён явно, публикация ограничена 127.0.0.1")

    https_enabled = is_truthy(env.get("HTTPS_ENABLED", ""))
    if mode == "production" and not https_enabled:
        problems.append(
            "HTTPS_ENABLED=True обязателен в production. Для HTTP-отладки используйте режим demo"
        )

    if not env.get("ALLOWED_HOSTS"):
        problems.append("ALLOWED_HOSTS пуст")
    else:
        for host in env["ALLOWED_HOSTS"].split(","):
            candidate = host.strip()
            if not candidate:
                continue
            if candidate == "*":
                if mode == "production":
                    problems.append(
                        "ALLOWED_HOSTS=* недопустим в production: любой Host-заголовок "
                        "будет принят. укажите домен явно"
                    )
                else:
                    warnings.append("ALLOWED_HOSTS=* принимает любой Host-заголовок")
                continue
            if validate_host(candidate)[1]:
                problems.append(f"ALLOWED_HOSTS содержит некорректное значение: {candidate!r}")

    if https_enabled:
        warnings.append(
            "HTTPS_ENABLED=True: TLS-терминатор и сертификат не проверяются скриптом. "
            "Прокси хоста должен сам перезаписывать X-Forwarded-Proto"
        )
    if not env.get("EMAIL_HOST_USER") or not env.get("EMAIL_HOST_PASSWORD"):
        warnings.append(
            "EMAIL_HOST_USER/EMAIL_HOST_PASSWORD пусты: воркер не сможет отправить "
            "код подтверждения, отправка будет отмечена как неудачная"
        )
    if mode == "production":
        warnings.append(
            "Режим production: фронтенд доступен только на 127.0.0.1; "
            "публичный доступ предоставляет TLS-прокси хоста"
        )

    if port is not None and port < 1024 and os.name != "nt":
        warnings.append(f"APP_PORT={port} требует привилегий root")

    return problems, warnings


# порт


def port_is_free(port: int) -> bool:
    """Preflight the exact host bindings Compose will request.

    every supported mode binds only IPv4 loopback so application traffic cannot
    bypass the host TLS proxy. the check remains advisory: another process can
    claim the port between this check and ``docker compose up``.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False

    # Windows can permit a wildcard bind alongside a listener that opted into
    # SO_REUSEADDR. Probe the loopbacks too, so an occupied local address is
    # never reported as free.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
    except OSError:
        pass
    return True


# docker

def run(command: list[str], *, capture: bool = False, timeout: int | None = DOCKER_TIMEOUT):
    try:
        return subprocess.run(
            command,
            capture_output=capture,
            text=True,
            cwd=str(ROOT),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CtlError(f"Команда превысила таймаут {timeout}с: {' '.join(command[:3])}") from error


def compose_files(mode: str) -> list[str]:
    files = ["-f", str(ROOT / COMPOSE_FILE)]
    if mode == "demo":
        files += ["-f", str(ROOT / DEMO_OVERRIDE)]
    return files


def compose(*args: str, mode: str | None = None, capture: bool = False, timeout=DOCKER_TIMEOUT):
    if mode is None:
        mode = resolve_mode(read_env(env_path()))
    command = [
        "docker", "compose",
        "-p", PROJECT_NAME,
        "--env-file", str(env_path()),
        *compose_files(mode),
        *args,
    ]
    return run(command, capture=capture, timeout=timeout)


def require_env() -> Path:
    path = env_path()
    if not path.exists():
        raise CtlError(f"{path.name} не найден. Выполните: projectctl.py init")
    if path.is_symlink():
        raise CtlError(f"{path.name} является символической ссылкой — это небезопасно")
    return path


def require_compose_files(mode: str) -> None:
    for index, value in enumerate(compose_files(mode)):
        if index % 2 == 1 and not Path(value).exists():
            raise CtlError(f"Не найден файл конфигурации Compose: {value}")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise CtlError("Docker не найден в PATH. Установите Docker и повторите.")
    if run(["docker", "info"], capture=True).returncode != 0:
        raise CtlError("Docker daemon не отвечает. Запустите Docker Desktop или службу docker.")
    if run(["docker", "compose", "version"], capture=True).returncode != 0:
        raise CtlError("Нужен Docker Compose v2 (команда 'docker compose').")


def parse_json_strict(payload: str, source: str):
    text = payload.strip()
    if not text:
        raise CtlError(f"{source} вернул пустой ответ — проверить конфигурацию невозможно")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise CtlError(f"{source} вернул неразбираемый JSON: {error.msg}") from error


def iter_json_objects(payload: str, source: str = "docker compose ps"):
    text = payload.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise CtlError(f"{source} вернул неразбираемый JSON: {error.msg}") from error
        return items
    return parsed if isinstance(parsed, list) else [parsed]


def check_service_exposure(mode: str) -> list[str]:
    result = compose("config", "--format", "json", mode=mode, capture=True)
    if result.returncode != 0:
        raise CtlError("docker compose config завершился с ошибкой. Проверьте compose.yaml и .env.")
    config = parse_json_strict(result.stdout, "docker compose config")
    if not isinstance(config, dict):
        raise CtlError("docker compose config вернул неожиданную структуру")

    services = config.get("services")
    if not isinstance(services, dict):
        raise CtlError("В выводе docker compose config нет раздела services")

    problems = []
    for name, service in services.items():
        ports = service.get("ports") or []
        if ports and name not in PUBLISHING_SERVICES:
            problems.append(f"Сервис '{name}' публикует порт наружу — так быть не должно")
        if name in PUBLISHING_SERVICES:
            for entry in ports:
                published = entry.get("host_ip") if isinstance(entry, dict) else None
                if published != "127.0.0.1":
                    problems.append(
                        f"'{name}' должен публиковаться только на 127.0.0.1, "
                        f"а не на {published or 'всех интерфейсах'}"
                    )
    return problems


# принадлежность сущностей


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def project_containers() -> list[tuple[str, str]]:
    result = run(
        [
            "docker", "ps", "-a",
            "--filter", f"label={COMPOSE_PROJECT_LABEL}={PROJECT_NAME}",
            "--format", '{{.Names}}\t{{.Label "' + COMPOSE_WORKDIR_LABEL + '"}}',
        ],
        capture=True,
    )
    if result.returncode != 0:
        raise CtlError("Не удалось перечислить контейнеры проекта")
    entries = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, workdir = line.partition("\t")
        entries.append((name.strip(), workdir.strip()))
    return entries


def project_volumes() -> list[str]:
    result = run(
        [
            "docker", "volume", "ls",
            "--filter", f"label={COMPOSE_PROJECT_LABEL}={PROJECT_NAME}",
            "--format", "{{.Name}}",
        ],
        capture=True,
    )
    if result.returncode != 0:
        raise CtlError("Не удалось перечислить тома проекта")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def project_networks() -> list[str]:
    result = run(
        [
            "docker", "network", "ls",
            "--filter", f"label={COMPOSE_PROJECT_LABEL}={PROJECT_NAME}",
            "--format", "{{.Name}}",
        ],
        capture=True,
    )
    if result.returncode != 0:
        raise CtlError("Не удалось перечислить сети проекта")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _inspect(kind: str, name: str, template: str) -> str:
    result = run(["docker", kind, "inspect", name, "--format", template], capture=True)
    fingerprint = result.stdout.strip() if result.returncode == 0 else ""
    if not fingerprint:
        raise CtlError(
            f"Не удалось получить сведения о ресурсе ({kind} {name}). "
            "Проверка владения невозможна, операция отменена."
        )
    return fingerprint


def resource_fingerprints() -> dict:
    return {
        "volumes": {
            name: _inspect("volume", name, "{{.CreatedAt}}|{{.Mountpoint}}")
            for name in project_volumes()
        },
        "networks": {
            name: _inspect("network", name, "{{.Id}}") for name in project_networks()
        },
    }


def record_ownership() -> None:
    write_state(resource_fingerprints())


def verify_recorded_resources() -> list[str]:
    """сверка в обе стороны: и появившиеся, и исчезнувшие ресурсы"""
    state = read_state()
    current = resource_fingerprints()
    if "resources" not in state:
        if any(current.get(kind) for kind in ("volumes", "networks")):
            return ["маркер владения создан прежней версией и не описывает ресурсы"]
        return []
    recorded = state.get("resources") or {}
    mismatches = []
    for kind, label in (("volumes", "том"), ("networks", "сеть")):
        known = recorded.get(kind) or {}
        actual = current.get(kind) or {}
        for name, fingerprint in actual.items():
            if name not in known:
                mismatches.append(f"{label} {name} не записан в маркере владения")
            elif known[name] != fingerprint:
                mismatches.append(f"{label} {name} пересоздан после присвоения")
        # Сети штатно удаляются командой down и создаются заново при up,
        # поэтому их исчезновение нормально. Том исчезнуть не должен: это
        # означало бы потерю данных.
        if kind == "volumes":
            for name in known:
                if name not in actual:
                    mismatches.append(f"{label} {name} исчез после присвоения")
    return mismatches


def check_ownership() -> list[str]:
    containers = project_containers()
    unproven = [
        f"{name} (создан из {workdir})" if workdir else f"{name} (каталог не указан)"
        for name, workdir in containers
        if not workdir or not _same_path(workdir, str(ROOT))
    ]
    if unproven:
        return [
            f"Имя проекта '{PROJECT_NAME}' занято контейнерами, принадлежность которых "
            f"этому каталогу не доказана: {', '.join(unproven)}. "
            "Операция отменена, чтобы не затронуть чужой стек."
        ]

    if workspace_owns_project():
        mismatches = verify_recorded_resources()
        if mismatches:
            return [
                "Ресурсы изменились с момента присвоения: "
                + "; ".join(mismatches)
                + ". Проверьте projectctl.py inspect. Если это ожидаемо, сбросьте "
                "маркер владения: projectctl.py adopt"
            ]
        return []

    leftovers = project_volumes() + project_networks()
    if not leftovers:
        return []
    return [
        f"Тома и сети проекта '{PROJECT_NAME}' уже существуют, но этот каталог не "
        f"подтверждён как их владелец: {', '.join(leftovers)}. Это могут быть данные "
        "другого стека. Посмотрите: projectctl.py inspect. Если данные ваши — "
        "выполните: projectctl.py adopt"
    ]


def project_publishes_port(port: int) -> bool:
    result = compose("ps", "--format", "json", capture=True)
    if result.returncode != 0:
        return False
    for item in iter_json_objects(result.stdout):
        for publisher in item.get("Publishers") or []:
            if publisher.get("PublishedPort") == port:
                return True
    return False


def wait_for_health(port: int, timeout: int) -> None:
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    deadline = time.monotonic() + timeout
    last_reason = "нет соединения"

    info(f"Жду HTTP 200 от {url}")
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                if response.status == 200:
                    ok("Сервис отвечает")
                    return
                last_reason = f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            last_reason = f"HTTP {error.code}"
            error.close()
        except (urllib.error.URLError, OSError):
            last_reason = "нет соединения"
        time.sleep(2)

    compose("logs", "--tail", "40", "backend")
    raise CtlError(f"Сервис не ответил за {timeout}с (последняя причина: {last_reason})")


def background_service_states() -> dict[str, str]:
    result = compose("ps", "--all", "--format", "json", capture=True)
    if result.returncode != 0:
        raise CtlError("docker compose ps завершился с ошибкой")
    return {
        item.get("Service", ""): item.get("Health") or item.get("State") or ""
        for item in iter_json_objects(result.stdout)
    }


def background_service_problems(states: dict[str, str]) -> dict[str, str]:
    return {
        name: states.get(name) or "контейнер не создан"
        for name, expected in BACKGROUND_SERVICES.items()
        if states.get(name) != expected
    }


def wait_for_background_services(timeout: int) -> None:
    deadline = time.monotonic() + timeout
    info("Жду готовности " + ", ".join(BACKGROUND_SERVICES))
    while True:
        problems = background_service_problems(background_service_states())
        if not problems:
            ok("Celery worker и beat работают")
            return
        failed = any(state in FAILED_SERVICE_STATES for state in problems.values())
        if failed or time.monotonic() >= deadline:
            compose("logs", "--tail", "40", *problems)
            details = "; ".join(f"{name}: {state}" for name, state in problems.items())
            raise CtlError(f"Фоновые сервисы не запустились ({details})")
        time.sleep(2)


# команды


def cmd_init(args: argparse.Namespace) -> int:
    path = env_path()

    if path.is_symlink():
        raise CtlError(
            f"{path.name} является символической ссылкой. "
            "Запись или чтение через неё небезопасны."
        )
    if path.exists():
        env = read_env(path)
        if not env.get("EMAIL_DELIVERY_QUOTA_SECRET"):
            # Existing deployments used SECRET_KEY as the quota HMAC key before
            # it became its own stable setting. Copying it once preserves the
            # currently active one-hour budget, while later key rotation leaves
            # that budget intact. The secret itself is never printed.
            legacy_secret = env.get("SECRET_KEY", "")
            if not validate_secret(legacy_secret):
                set_env_value(path, "EMAIL_DELIVERY_QUOTA_SECRET", legacy_secret)
                ok("Добавлен EMAIL_DELIVERY_QUOTA_SECRET для стабильной квоты писем")
            else:
                warn(
                    "EMAIL_DELIVERY_QUOTA_SECRET отсутствует, а SECRET_KEY нельзя "
                    "безопасно перенести. Заполните оба секрета вручную."
                )
        if not args.rotate_secret:
            info(f"{ENV_FILE} уже существует")
            info("Для смены ключа: projectctl.py init --rotate-secret")
            return 0
        set_env_value(path, "SECRET_KEY", generate_secret())
        ok("SECRET_KEY заменён. Все активные сессии станут недействительны")
        return 0

    host = ""
    if args.mode == "production":
        if not args.host:
            raise CtlError("Для режима production укажите домен: --host example.com")
        host, host_problems = validate_host(args.host)
        if host_problems:
            raise CtlError("; ".join(host_problems))

    if args.mode == "production" and args.allow_debug:
        raise CtlError(
            "DEBUG=True недопустим в режиме production: стек публикуется наружу. "
            "Используйте --mode demo"
        )

    port, port_problems = validate_port(str(args.port))
    if port_problems:
        raise CtlError("; ".join(port_problems))

    https = args.mode == "production"
    write_env_atomic(
        path,
        render_env(mode=args.mode, host=host, port=port, debug=args.allow_debug, https=https),
    )

    ok(f"Создан {ENV_FILE} (режим: {args.mode}, порт: {port})")
    ok("SECRET_KEY, EMAIL_DELIVERY_QUOTA_SECRET и DB_PASSWORD сгенерированы и не выводятся")
    if os.name == "nt":
        warn("Windows: ограничьте доступ к .env через icacls (см. scripts/README.md)")
    else:
        ok("Права на файл: 600")
    if args.mode == "demo":
        ok("Режим demo: фронтенд публикуется только на 127.0.0.1")
    if args.allow_debug:
        warn("DEBUG=True разрешён только в demo: публикация остаётся на loopback")
    info(
        "Заполните EMAIL_HOST_USER/EMAIL_HOST_PASSWORD, иначе письмо не подтвердится "
        "и регистрация вернёт 202"
    )
    info("Дальше: projectctl.py validate")
    return 0


def render_env(*, mode: str, host: str, port: int, debug: bool, https: bool) -> str:
    scheme = "https" if https else "http"
    if mode == "production":
        allowed_hosts = host
        origins = f"{scheme}://{host}"
        trusted_proxy_hops = 2
    else:
        allowed_hosts = "localhost,127.0.0.1"
        origins = f"http://localhost:{port},http://127.0.0.1:{port}"
        trusted_proxy_hops = 1

    return f"""# Создан scripts/projectctl.py. Не коммитить.
# SECRET_KEY меняйте только через --rotate-secret; quota-secret оставляйте стабильным.

PROJECTCTL_MODE={mode}
SECRET_KEY={generate_secret()}
# Стабильный ключ квоты: не меняется при init --rotate-secret.
EMAIL_DELIVERY_QUOTA_SECRET={generate_secret()}
DEBUG={debug}
HTTPS_ENABLED={https}
ALLOWED_HOSTS={allowed_hosts}
CSRF_TRUSTED_ORIGINS={origins}
APP_PORT={port}
# production: TLS-прокси хоста + nginx контейнера; demo: nginx контейнера
NINJA_NUM_PROXIES={trusted_proxy_hops}

DB_NAME=steinsgate
DB_USER=steinsgate
DB_PASSWORD={generate_secret()}
DB_HOST=127.0.0.1
DB_PORT=5432

# Заполните, иначе письмо с кодом не подтвердится (регистрация вернёт 202).
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=465
EMAIL_TIMEOUT=10
EMAIL_USE_SSL=True
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
"""


def cmd_validate(args: argparse.Namespace, *, check_port: bool = True) -> int:
    path = require_env()

    require_docker()
    ok("Docker и Compose v2 доступны")

    env = read_env(path)
    mode = resolve_mode(env)
    require_compose_files(mode)
    problems, warnings = validate_env_values(env, allow_debug=args.allow_debug)
    problems.extend(check_ownership())

    port, _ = validate_port(env.get("APP_PORT", ""))
    if check_port and port is not None and not port_is_free(port):
        if project_publishes_port(port):
            warnings.append(f"Порт {port} занят контейнером этого же проекта (стек уже запущен)")
        else:
            problems.append(
                f"Порт {port} занят на 127.0.0.1. "
                f"Освободите его или задайте другой APP_PORT в {ENV_FILE}"
            )

    problems.extend(check_service_exposure(mode))
    ok(f"compose config разобран без ошибок (режим: {mode})")

    volumes = project_volumes()
    if volumes:
        warnings.append(
            f"Существующие тома будут переиспользованы: {', '.join(volumes)}. "
            "Данные предыдущего запуска сохранятся. Просмотр: projectctl.py inspect"
        )

    for message in warnings:
        warn(message)
    if problems:
        print()
        for message in problems:
            print(f"[x] {message}")
        raise CtlError(f"Проверка не пройдена: {len(problems)} проблем(ы)")

    ok("Конфигурация корректна")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    cmd_validate(args, check_port=False)

    env = read_env(env_path())
    mode = resolve_mode(env)
    port, _ = validate_port(env.get("APP_PORT", ""))
    if port is None:
        raise CtlError("APP_PORT не удалось разобрать")

    build_args = [] if args.no_build else ["--build"]
    info(f"Запускаю стек '{PROJECT_NAME}' (режим: {mode})")
    result = compose("up", "-d", *build_args, mode=mode, timeout=BUILD_TIMEOUT)
    if result.returncode != 0:
        if not port_is_free(port) and not project_publishes_port(port):
            raise CtlError(
                f"Не удалось запустить: порт {port} занят другим процессом "
                "(освободился между проверкой и стартом)"
            )
        raise CtlError("Не удалось запустить сервисы. Смотрите: projectctl.py logs")

    record_ownership()

    if not project_publishes_port(port):
        raise CtlError(
            f"Порт {port} не опубликован контейнером проекта '{PROJECT_NAME}'. "
            "Возможно, на нём отвечает посторонний сервис"
        )

    wait_for_health(port, args.timeout)
    wait_for_background_services(args.timeout)
    ok(f"Готово: http://localhost:{port} (публикация: 127.0.0.1)")
    return 0


def cmd_down(_args: argparse.Namespace) -> int:
    require_docker()
    require_env()
    problems = check_ownership()
    if problems:
        raise CtlError(problems[0])

    info(f"Останавливаю стек '{PROJECT_NAME}' (тома сохраняются)")
    if compose("down").returncode != 0:
        raise CtlError("Не удалось остановить сервисы")
    ok("Контейнеры остановлены, данные на месте")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    require_docker()
    require_env()
    ownership_problems = check_ownership()
    if ownership_problems:
        raise CtlError(ownership_problems[0])

    result = compose("ps")
    if result.returncode != 0:
        raise CtlError("docker compose ps завершился с ошибкой")

    env = read_env(env_path())
    port, _ = validate_port(env.get("APP_PORT", ""))
    if port is None:
        warn("APP_PORT не задан, health-check пропущен")
        return 1
    if not project_publishes_port(port):
        raise CtlError(
            f"Порт {port} не опубликован контейнером проекта '{PROJECT_NAME}'. "
            "HTTP-ответ от другого процесса не считается статусом этого стека."
        )
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=5
        ) as response:
            if response.status != 200:
                raise CtlError(f"HTTP {response.status} от {HEALTH_PATH}")
            ok(f"HTTP {response.status} от {HEALTH_PATH}")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
        raise CtlError(f"{HEALTH_PATH} не отвечает на порту {port}") from error

    problems = background_service_problems(background_service_states())
    if problems:
        details = "; ".join(f"{name}: {state}" for name, state in problems.items())
        raise CtlError(f"Фоновые сервисы не в рабочем состоянии ({details})")
    ok("Celery worker и beat работают")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    require_docker()
    require_env()
    command = ["logs", "--tail", str(args.tail)]
    if args.follow:
        command.append("--follow")
    if args.service:
        command.append(args.service)
    result = compose(*command, timeout=None if args.follow else DOCKER_TIMEOUT)
    if result.returncode != 0:
        raise CtlError("docker compose logs завершился с ошибкой")
    return 0


def cmd_inspect(_args: argparse.Namespace) -> int:
    require_docker()
    containers = project_containers()
    networks = project_networks()
    volumes = project_volumes()

    info(f"Сущности проекта '{PROJECT_NAME}' (только просмотр, ничего не удаляется)")

    print("\nКонтейнеры:")
    if not containers:
        print("  нет")
    for name, workdir in containers:
        origin = workdir or "каталог неизвестен"
        mark = "" if not workdir or _same_path(workdir, str(ROOT)) else "  <-- чужой каталог"
        print(f"  {name}  [{origin}]{mark}")

    print("\nСети:")
    print("  нет" if not networks else "\n".join(f"  {name}" for name in networks))

    print("\nТома:")
    print("  нет" if not volumes else "\n".join(f"  {name}" for name in volumes))
    if volumes:
        print("\n  Следующий 'up' переиспользует эти тома вместе с их данными.")
        print("  Скрипт их не удаляет. Удалить вручную (данные будут потеряны):")
        print(f"    docker volume rm {' '.join(volumes)}")

    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    require_docker()

    unproven = [
        f"{name} (создан из {workdir})" if workdir else f"{name} (каталог не указан)"
        for name, workdir in project_containers()
        if not workdir or not _same_path(workdir, str(ROOT))
    ]
    if unproven:
        raise CtlError(
            "Присвоение отменено: принадлежность контейнеров этому каталогу не доказана: "
            + ", ".join(unproven)
        )

    volumes = project_volumes()
    networks = project_networks()

    if volumes or networks:
        warn("Присвоение объявит эти ресурсы принадлежащими текущему каталогу.")
        warn("Если это данные другого стека, они будут использованы как свои.")
    else:
        warn("Ресурсов проекта сейчас нет: маркер владения будет сброшен.")
        warn("Если ранее записанный том исчез, его данные уже потеряны.")
    print()
    print(f"  каталог: {ROOT}")
    for name in volumes:
        print(f"  том:     {name}")
    for name in networks:
        print(f"  сеть:    {name}")

    if not args.yes:
        try:
            answer = input("Введите ADOPT для подтверждения: ").strip()
        except EOFError:
            raise CtlError(
                "Подтверждение невозможно в неинтерактивном режиме, используйте --yes"
            ) from None
        if answer != "ADOPT":
            raise CtlError("Присвоение отменено")

    record_ownership()
    ok(f"Ресурсы записаны в {STATE_FILE} вместе с идентификаторами")
    return 0


# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projectctl.py",
        description=f"Подготовка и запуск проекта {PROJECT_NAME}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help=f"создать {ENV_FILE}, если его нет")
    init.add_argument("--mode", choices=("demo", "production"), default="demo")
    init.add_argument("--host", help="домен для режима production")
    init.add_argument("--port", type=int, default=DEFAULT_PORT)
    init.add_argument("--rotate-secret", action="store_true", help="заменить SECRET_KEY")
    init.add_argument("--allow-debug", action="store_true", help="записать DEBUG=True")
    init.set_defaults(func=cmd_init)

    validate = subparsers.add_parser("validate", help="проверить окружение и конфигурацию")
    validate.add_argument("--allow-debug", action="store_true")
    validate.set_defaults(func=cmd_validate)

    up = subparsers.add_parser("up", help="запустить сервисы после успешной проверки")
    up.add_argument("--allow-debug", action="store_true")
    up.add_argument("--no-build", action="store_true")
    up.add_argument("--timeout", type=int, default=HEALTH_TIMEOUT)
    up.set_defaults(func=cmd_up)

    down = subparsers.add_parser("down", help="остановить сервисы проекта, тома не трогать")
    down.set_defaults(func=cmd_down)

    status = subparsers.add_parser("status", help="состояние контейнеров и health-check")
    status.set_defaults(func=cmd_status)

    logs = subparsers.add_parser("logs", help="логи сервисов проекта")
    logs.add_argument("service", nargs="?")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    inspect = subparsers.add_parser("inspect", help="показать контейнеры, сети и тома проекта")
    inspect.set_defaults(func=cmd_inspect)

    adopt = subparsers.add_parser(
        "adopt", help="признать существующие тома и сети принадлежащими этому каталогу"
    )
    adopt.add_argument("--yes", action="store_true", help="без интерактивного подтверждения")
    adopt.set_defaults(func=cmd_adopt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CtlError as error:
        sys.stdout.flush()
        print(f"\n[x] {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        sys.stdout.flush()
        print("\nПрервано пользователем", file=sys.stderr)
        return 130
    except OSError as error:
        sys.stdout.flush()
        print(f"[x] Ошибка файловой системы: {error.strerror or error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
