# projectctl

CLI на стандартной библиотеке Python для подготовки `.env` и управления стеком через Docker Compose. Работает на Windows и Linux.

```
scripts/projectctl.py init | validate | up | down | status | logs | inspect | adopt
```

Требования: Docker Engine с Compose v2, Python 3.10+.

## Режимы и периметр

Режим задается при `init` и хранится в `.env` как `PROJECTCTL_MODE`. Неизвестное значение считается ошибкой.

| | `--mode demo` | `--mode production` |
|---|---|---|
| Публикация фронтенда | `127.0.0.1:APP_PORT` через `compose.demo.yaml` | `127.0.0.1:APP_PORT` |
| Внешний доступ | нет | только через TLS-прокси хоста |
| `HTTPS_ENABLED` | `False` | `True`, обязательно |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | домен из `--host`, `*` запрещен |
| `NINJA_NUM_PROXIES` | `1` | `2` (TLS-прокси и nginx) |
| `DEBUG=True` | только с `--allow-debug` | запрещен |

Порты публикует только `frontend`. PostgreSQL, оба инстанса Redis, backend и контейнеры Celery доступны только во внутренних сетях Compose; `validate` проверяет это по выводу `docker compose config`.

TLS-терминатор на хосте принимает HTTPS, перезаписывает `X-Forwarded-For` и `X-Forwarded-Proto` и отдает HSTS. Сертификат и доступность HTTPS скрипт не проверяет.

## Быстрый старт

Ubuntu:

```bash
python3 scripts/projectctl.py init
nano .env                                   # EMAIL_HOST_USER, EMAIL_HOST_PASSWORD
python3 scripts/projectctl.py validate
python3 scripts/projectctl.py up
```

Windows PowerShell:

```powershell
python scripts\projectctl.py init
notepad .env
python scripts\projectctl.py validate
python scripts\projectctl.py up
```

Сервер:

```bash
python3 scripts/projectctl.py init --mode production --host steins.example.com
```

## Команды

| Команда | Действие | Код возврата |
|---|---|---|
| `init` | Создает `.env`. В существующий файл только добавляет отсутствующий `EMAIL_DELIVERY_QUOTA_SECRET` | 1 при ошибке аргументов |
| `validate` | Docker, файлы Compose, `.env`, секреты, порт, публикация сервисов, владение ресурсами | 1 при любой проблеме |
| `up` | `validate`, затем `compose up -d --build`; ожидание HTTP 200 от `/api/anime`, healthy `celery-worker` и running `celery-beat` | 1 при сбое запуска, health-check или фоновых сервисов |
| `down` | Останавливает контейнеры и сети, тома сохраняются | 1 при сбое |
| `status` | `compose ps`, health-check, состояние `celery-worker` и `celery-beat` | 1 если API не отвечает или фоновые сервисы не работают |
| `logs` | Логи сервисов | 1 при сбое Compose |
| `inspect` | Контейнеры, сети и тома проекта, ничего не удаляет | 0 |
| `adopt` | Записывает текущие тома и сети как ресурсы этого каталога | 1 без подтверждения |

Флаги:

| Команда | Флаг | Назначение |
|---|---|---|
| `init` | `--mode demo\|production` | Режим, по умолчанию `demo` |
| `init` | `--host` | Домен, обязателен для `production` |
| `init` | `--port` | Порт фронтенда, по умолчанию 4173 |
| `init` | `--rotate-secret` | Заменить `SECRET_KEY` в существующем `.env` |
| `init`, `validate`, `up` | `--allow-debug` | Разрешить `DEBUG=True` в `demo` |
| `up` | `--no-build`, `--timeout` | Без пересборки образов; секунды ожидания health-check |
| `logs` | `--tail N`, `--follow`, имя сервиса | Фильтрация вывода |
| `adopt` | `--yes` | Без интерактивного подтверждения |

## Изоляция

Имя Compose-проекта `steinsgate_mailor` зашито в код и не переопределяется через окружение. Все команды работают только с ресурсами этого проекта.

Состояние Docker меняют две команды: `compose up -d [--build]` и `compose down`. Остальные вызовы только читают. Скрипт не удаляет тома и образы, не выполняет `prune` и `down -v`. На диск пишет `.env`, `.projectctl-state.json` и временный файл атомарной записи в корне проекта.

## Проверки `.env`

- `SECRET_KEY`, `EMAIL_DELIVERY_QUOTA_SECRET` и `DB_PASSWORD` генерируются через `secrets.token_urlsafe(64)` и не выводятся в консоль и сообщения об ошибках.
- Значения, которые интерполирует Compose (`SECRET_KEY`, `EMAIL_DELIVERY_QUOTA_SECRET`, `DB_PASSWORD`, `EMAIL_HOST_PASSWORD`), не должны содержать `$`, кавычки, переводы строк и краевые пробелы. `$` Compose трактует как подстановку и передает в контейнер другое значение.
- Существующий `.env` не перезаписывается. `SECRET_KEY` меняется только через `init --rotate-secret`, `EMAIL_DELIVERY_QUOTA_SECRET` при этом сохраняется.
- Запись атомарная: временный файл, `fsync`, `os.replace`. Права `600` выставляются до подстановки файла, запись по символической ссылке запрещена. На Windows права ограничиваются вручную:

```powershell
icacls .env /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

- `--host` принимает только hostname или IP без схемы, порта, пути, пробелов и управляющих символов.
- Все вызовы Compose получают `--env-file` с тем же файлом, который проверяет скрипт.

## Владение ресурсами

Перед `up` и `down` проверяется, что контейнеры, тома и сети проекта принадлежат этому каталогу.

- **Контейнеры** несут метку рабочего каталога Compose. Контейнер без метки или с другим каталогом блокирует операцию.
- **Тома и сети** такой метки не имеют. Их принадлежность подтверждает `.projectctl-state.json` с отпечатками: `CreatedAt` и точка монтирования для тома, `Id` для сети. Отпечаток обнаруживает пересоздание ресурса, но не подмену содержимого тома.
- Сверка двусторонняя: блокирует и незаписанный ресурс, и исчезнувший записанный том. Исчезновение сети допустимо, ее удаляет `down`.
- Маркер записывается сразу после успешного `compose up` или командой `adopt`. `validate` маркер не записывает.

`adopt` показывает ресурсы перед записью и отказывается работать, если контейнеры созданы из другого каталога. При отсутствии ресурсов сбрасывает маркер.

```bash
python3 scripts/projectctl.py adopt          # запросит слово ADOPT
python3 scripts/projectctl.py adopt --yes
```

## Сообщения проверок

| Сообщение | Причина и действие |
|---|---|
| `SECRET_KEY содержит '$'` (и другие секреты) | Сгенерировать значение заново; для `SECRET_KEY` `init --rotate-secret` |
| `DEBUG=True недопустим в режиме production` | Отладка только в `demo` с `--allow-debug` |
| `HTTPS_ENABLED=True обязателен в production` | Установить `HTTPS_ENABLED=True` и настроить TLS-прокси |
| `ALLOWED_HOSTS=* недопустим в production` | Указать домен |
| `PROJECTCTL_MODE=... не распознан` | Допустимо `demo` или `production` |
| `Сервис 'X' публикует порт наружу` | Убрать `ports` у сервиса в `compose.yaml` |
| `docker compose config вернул неразбираемый JSON` | Обновить Docker Compose до актуальной v2 |
| `занято контейнерами, принадлежность которых этому каталогу не доказана` | Имя проекта занято другим стеком; `inspect` |
| `уже существуют, но этот каталог не подтвержден как их владелец` | Ресурсы от другого стека или без маркера; если данные этого каталога, `adopt` |
| `Ресурсы изменились с момента присвоения` | Том или сеть пересозданы либо том удален; `inspect`, затем `adopt` |
| `Фоновые сервисы не запустились` | Воркер или Beat завершились, перезапускаются или не прошли healthcheck; `logs celery-worker` |
| `Фоновые сервисы не в рабочем состоянии` | Воркер потерял брокер (heartbeat устарел) или Beat остановлен; `status`, `logs` |
| `EMAIL_HOST_USER/EMAIL_HOST_PASSWORD пусты` | Предупреждение: воркер отметит отправку кода как неудачную |

## Тесты

```bash
python -m unittest discover -s scripts -p "test_*.py"
PROJECTCTL_INTEGRATION_UP=1 python -m unittest discover -s scripts -p "test_*.py"
```

Юнит-тесты Docker не требуют. Интеграционные тесты используют временное имя проекта, свободный порт и отдельный env-файл во временном каталоге; корневой `.env` не читается. `PROJECTCTL_INTEGRATION_UP=1` включает полный цикл `up -> health-check -> down` с проверкой `celery-worker`, `celery-beat` и завершившегося `migrate`, в CI он включен.

## Ограничения

- TLS, сертификат и доступность HTTPS не проверяются.
- Параллельные запуски `up` не блокируются.
- Лимиты ресурсов на время сборки не задаются.
- Доступ к Docker daemon эквивалентен root-доступу к хосту.
