# Нагрузочное тестирование

Проверка устойчивости связки django-ninja, PostgreSQL и Redis под параллельной нагрузкой и поиск N+1.

| Компонент | Назначение |
|---|---|
| `backend/loadtest/locustfile.py` | Сценарий сессии зрителя: каталог, страница тайтла, комментарии, оценки, реакции, прогресс, регистрация |
| `backend/loadtest/run_loadtest.sh` | Полный прогон в изолированном стеке |
| `accounts/management/commands/seed_loadtest.py` | Наполнение БД тестовыми пользователями и комментариями |
| `accounts/management/commands/profile_queries.py` | Подсчёт SQL-запросов на ручках |
| `compose.loadtest.yaml` | Оверлей стека для прогона |

## Предохранители

- `seed_loadtest` и `profile_queries` работают только при `LOADTEST=1` (или с `--force`). Оверлей `compose.loadtest.yaml` выставляет переменную в контейнере `backend`, боевой `compose.yaml` её не задаёт.
- Сид создаёт и удаляет только объекты с префиксом `loadtest_`.
- `profile_queries` создаёт данные во временной транзакции с откатом и очищает кэш каталога до и после.
- Locust не запускается против хоста вне `localhost`, `127.0.0.1`, `::1`, `backend` без `LOADTEST_ALLOW_REMOTE=1`.

## Оверлей `compose.loadtest.yaml`

- `DEBUG=False`, `HTTPS_ENABLED=False`.
- Лимиты запросов подняты до значений, которые не ограничивают прогон.
- Почтовый бэкенд `locmem` у `celery-worker`: регистрация не отправляет настоящих писем.
- gunicorn `gthread`: `GUNICORN_WORKERS=4`, `GUNICORN_THREADS=8`.
- `LOADTEST=1`.

## Полный прогон

Требуется запущенный Docker и `.env` в корне репозитория.

```bash
backend/loadtest/run_loadtest.sh                          # ступени 50, 200, 500 по 60 с
backend/loadtest/run_loadtest.sh --stages "100 500 1000" --time 90s
backend/loadtest/run_loadtest.sh --users 1000 --comments 300
backend/loadtest/run_loadtest.sh --keep                   # оставить стек и данные
backend/loadtest/run_loadtest.sh --down                   # остановить стек и удалить его тома
```

Скрипт:

1. Поднимает стек под Compose-проектом `steinsgate_loadtest` на порту `4273` (`LOADTEST_APP_PORT`), включая `redis-broker`, `celery-worker` и `celery-beat`. Основной стек на `4173` не затрагивается.
2. Ждёт HTTP 200 от `/api/anime`.
3. Выполняет `seed_loadtest` и `profile_queries`, профиль сохраняется в `loadtest_out/query_profile.txt`.
4. Запускает Locust на хосте для каждой ступени с рэмпом `users / 10` в секунду.
5. Выводит сводку RPS, p95, p99 и доли ошибок.
6. Выполняет `down -v` для проекта `steinsgate_loadtest`, если не указан `--keep`.

Отчёты CSV и HTML сохраняются в `loadtest_out/` (в `.gitignore`).

## Baseline

gunicorn `gthread` 4 x 8 без перезапуска воркеров по `--max-requests`, PostgreSQL 16, Redis 7, Celery, nginx. SMTP воркера во время прогона не отвечал (таймаут 5 с): регистрация публикует задачу и не ждёт почтовый сервер. Ступени 50 и 200 по 60 с, 500 по 90 с, рэмп `users / 10` в секунду. Разовый прогон на dev-машине, значения зависят от железа.

| Пользователей | RPS | p50 | p95 | p99 | Ошибки | Регистрация p50 / p95 |
|---|---|---|---|---|---|---|
| 50 | 28.9 | 23 ms | 220 ms | 2100 ms | 0% | 200 / 250 ms |
| 200 | 113.0 | 23 ms | 370 ms | 2100 ms | 0% | 210 / 270 ms |
| 500 | 268.9 | 37 ms | 1300 ms | 2500 ms | 0% | 280 / 1300 ms |

p99 на всех ступенях определяют первые запросы пользователей в момент рэмпа (`/api/auth/csrf`, `/api/auth/login`). Время регистрации складывается из хеширования пароля PBKDF2 и не зависит от доступности SMTP.

Профиль запросов: страница комментариев выполняет постоянное число SQL-запросов независимо от числа комментариев, каталог отдаётся из кэша.

## Ручной прогон

Против стека, запущенного с оверлеем без изоляции скрипта:

```bash
docker compose -f compose.yaml -f compose.loadtest.yaml up -d --build
docker compose -f compose.yaml -f compose.loadtest.yaml exec backend python manage.py seed_loadtest --users 500 --comments 150

LOADTEST_USERS=500 locust -f backend/loadtest/locustfile.py --host http://localhost:4173 --headless -u 500 -r 25 -t 5m --csv=loadtest_out/manual --html=loadtest_out/manual.html
```

Профиль SQL-запросов без нагрузки:

```bash
docker compose -f compose.yaml -f compose.loadtest.yaml exec backend python manage.py profile_queries --verbose-sql
```

## Метрики отчёта

- **p95 / p99** по ручке: рост указывает на узкое место.
- **Доля ошибок**: 5xx под нагрузкой указывают на дедлоки, исчерпание пула соединений или таймауты.
- **Плато RPS**: отсутствие роста при добавлении пользователей означает достигнутый потолок.
- Запросы из `profile_queries` разбираются через `EXPLAIN ANALYZE`.

## Очистка

```bash
docker compose -p steinsgate_loadtest -f compose.yaml -f compose.loadtest.yaml down -v
```

Удаление данных сида из стека без остановки:

```bash
docker compose -f compose.yaml -f compose.loadtest.yaml exec backend python manage.py seed_loadtest --flush
```

Сид пересчитывает `likes_count` и `dislikes_count` своих комментариев и прибавляет созданные просмотры к `total_views`. `--flush` вычитает из `total_views` удаляемые просмотры. Засеянные просмотры, которые ротация уже удалила (старше суток), вычесть нечем, они остаются в счётчике.
