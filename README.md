# Steins;Gate Fan Platform

Fan site for Steins;Gate: both seasons, the special episode and the movie, user accounts, ratings, watch progress and comments.

React SPA, Django + django-ninja API with a service layer, PostgreSQL, Redis, Celery, Docker, nginx.

## Architecture

```
browser
   |
   v
nginx (frontend container, :4173)
   |                    security headers + CSP
   |-- /                SPA; index.html is revalidated on every load
   |-- /assets/         hashed bundles, cached as immutable
   |-- /img/            posters and backgrounds (WebP)
   |-- /static/         Django admin static (shared volume)
   |-- /media/          user uploads (shared volume)
   |-- /api/, /admin/ --> backend (gunicorn, :8000)
                             |
          +------------------+--------------------+
          v                  v                    v
    PostgreSQL 16         Redis 7            redis-broker
          ^           cache, throttling,     Celery queues
          |             IP lockout          AOF, noeviction
          |                                   |        ^
          |                                   v        |
          +------------------------------ celery-worker  celery-beat
                                              |
                                              v
                                            SMTP
```

- `frontend/` - React SPA, layers `app -> pages -> features -> entities -> shared`. API types are generated from the backend OpenAPI schema.
- `backend/` - Django apps with a service layer. Endpoints and Celery tasks validate input and delegate to services.
- `compose.yaml` - `db`, `redis`, `redis-broker`, `migrate` (one-shot), `backend`, `celery-worker`, `celery-beat`, `frontend`.

Title metadata ships in the frontend bundle, runtime data comes from the API. See [Hybrid data model](#hybrid-data-model-and-adapter).

## Tech stack

| Layer    | Technology |
|----------|------------|
| Frontend | React 19, TypeScript (strict), Vite, Tailwind CSS 4, React Router 7, TanStack Query |
| Backend  | Python 3.14, Django 6, django-ninja, gunicorn, Celery 5.6 |
| Storage  | PostgreSQL 16 (SQLite for local development), Redis 7 |
| Infra    | Docker, docker compose, nginx |
| Quality  | ruff, ESLint, Django tests, projectctl integration tests, GitHub Actions, Gitleaks |

## API

The specification is served at `/api/docs` (Swagger UI) and `/api/openapi.json` when `API_DOCS_ENABLED` is on. The same schema is committed as `frontend/openapi.json` and is the source for `frontend/src/shared/api/types.gen.ts` (`npm run gen:api`).

## Security

### Accounts

- Session authentication with HttpOnly cookies. Routes without cookie auth (`register`, `login`, `verify-email`, `resend-verification`, `view`) call `check_csrf` explicitly.
- A new account is inactive until the 6-digit email code is confirmed. Code TTL is 15 minutes, 5 attempts. The database stores an HMAC hash and a nonce, not the code.
- `register` returns `201` and `resend-verification` returns `200` once the transaction commits. The letter is sent by the Celery worker; the request does not wait for SMTP. A still valid code is resent unchanged.
- An expired unconfirmed registration releases its username and email. Disabled accounts are excluded from cleanup.
- One address receives at most 6 verification letters per hour across registrations and resends. The counter is keyed by an HMAC of the address.
- Rotating `SECRET_KEY` invalidates pending codes and sessions.
- `auth_user.email` has a partial case-insensitive unique index.
- The Django admin redirects anyone it does not admit to `/steins-gate`, its own login form included, the same response an unmatched frontend route gets from the SPA router. Staff sign in on the site through `/api/auth/login`, which has the IP lockout and throttles, and the same session opens `/admin/` (`config/middleware.py`).
- Registration names a taken username or email explicitly. Usernames are public in comments, and hiding a taken email needs a letter to its owner instead of an error, which the site does not send.

### Abuse control

- The client IP is read from the trusted right side of `X-Forwarded-For`: one hop in demo, two in production (host TLS proxy and compose nginx). An IPv6 client is its /64 network, the block a provider assigns to one subscriber, so rotating addresses inside it does not reset the lockout, the throttles or view deduplication.
- Auth endpoints, writes, view events and watch progress have per-minute and per-hour limits, resend has an hourly limit. Every limit is a fixed window counted by an atomic increment in Redis, so the count is the same for all gunicorn workers and threads. django-ninja keeps its own counter on the throttle object, which one process shares between concurrent requests, so the project replaces that part.
- IP lockout on login and code entry: 5 failures block for 30 seconds, every fourth series blocks for 10 minutes, success resets the counter.
- With Redis unavailable, writes and view events continue without limits. Registration, login, code verification and resend return `503`. Attempt and resend limits in PostgreSQL still apply.

### Input and uploads

- Every route has an explicit Pydantic schema; nothing is bound from models. Comments pass a link and profanity filter.
- Avatars are validated by decoding the image with size and pixel limits. The previous file is deleted on replacement.

### Configuration and perimeter

- `DEBUG` defaults to `False`. Startup fails without `SECRET_KEY`.
- `HTTPS_ENABLED` switches the https redirect, Secure cookies and HSTS together. In production the application listens on loopback, the host TLS proxy is the only entry point.
- Only `frontend` publishes a port. PostgreSQL, both Redis instances and the Celery containers are on an internal network; the worker reaches SMTP through a separate egress network and is the only Django process that gets the SMTP password.
- Containers run with `no-new-privileges`. Backend, Celery and nginx drop all capabilities and run as non-root. Celery messages are JSON only and carry `user_id` and a dispatch token, never the code.
- nginx sets `nosniff`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`, a CSP for the SPA and `default-src 'none'; sandbox` for uploaded files. Django's `SecurityMiddleware` sets headers for `/api/` and `/admin/`.

## Caching and logging

Redis caches the average rating and the title list. A view is registered by a CSRF-protected `POST`. PostgreSQL decides under an advisory lock whether the view is new within 24 hours; Redis keeps a marker for the rest of that window. User-specific data is not cached.

Containers log to stdout (`LOG_TO_FILES=False` in the image); rotation is handled by the Docker `local` logging driver. Local runs write `application.log`, `security.log` and `error.log` to `backend/logs/`, `access.log` only under `runserver`.

## Background tasks

| Task | Queue | Trigger |
|------|-------|---------|
| `send_verification_code` | `email` | registration and resend, after commit |
| `reconcile_verification_delivery` | `email` | Beat, every minute |
| `purge_expired_registrations` | `maintenance` | Beat, hourly, batches of 1000 until the backlog is empty |
| `purge_view_history` | `maintenance` | Beat, hourly, deletes views older than the 24-hour window in batches of 5000, at most 100 batches per run |
| `clear_expired_sessions` | `maintenance` | Beat, daily |

- **Retries:** SMTP 4xx replies, disconnects, timeouts and connection errors are retried up to 5 times with exponential backoff (15 s base, 180 s cap, full jitter). 5xx replies and authentication errors set `delivery_failed_at` without retry. The retry window fits into the 15-minute code TTL, an expired code is not sent.
- **Worker:** one container, queues `email,maintenance`, prefork pool with concurrency 2, `acks_late`, `reject_on_worker_lost`, prefetch 1, hard time limit 60 s, results ignored, 30 letters per minute.
- **Broker:** separate Redis instance with AOF (`everysec`) and `noeviction`. The cache instance uses `volatile-lru`. Memory limit and eviction policy apply to a whole Redis instance, so the two cannot share one.
- **Health:** the worker updates a heartbeat file on tmpfs every 15 seconds while it is connected to the broker; the Docker healthcheck requires it to be younger than a minute. `projectctl up` waits for a healthy worker and a running Beat, `projectctl status` fails otherwise.

## Architecture decisions

### Hybrid data model and adapter

**Where:** `frontend/src/shared/config/animes.ts`, `frontend/src/entities/anime/model.ts`, `backend/catalog/schemas.py::AnimeStatsOut`.

**Problem:** the four titles are fixed between releases, while ratings, view counts, progress and comments change at runtime. Serving both from one endpoint sends static text on every page view and adds a request before the first render.

**Solution:** name, season, type, genres, description, poster and player links ship in the bundle. `GET /api/anime/{slug}` returns `slug`, `avg_rating`, `total_views` and `user_rating`. `useAnime` joins both sources by slug and is the only place where they meet; the page passes the stats down, `useRateAnime` writes vote results into the same query key.

**Trade-off:** title metadata updates require a frontend build and deployment. Database rows remain the catalog identity for slug uniqueness and foreign keys.

### Service layer and thin controllers

**Where:** `backend/{accounts,catalog,comments,watch}/services.py`, `backend/accounts/tasks.py`.

**Problem:** business rules inside HTTP handlers or task bodies are unreachable for other entry points and require a request or a worker in tests.

**Solution:** endpoints, Celery tasks and management commands parse input, call service functions and map domain exceptions to status codes or retries. Most tests call services directly.

### Strict DTO and boundary validation

**Where:** `backend/*/schemas.py`.

**Problem:** binding request data to ORM models widens the API with every new model field and allows mass assignment.

**Solution:** every route declares an input and output schema. Bounds are enforced at the boundary, for example `watch/schemas.py::ProgressIn` rejects IEEE 754 infinity and NaN, which `json.loads` accepts and which would otherwise be stored and serialized as invalid JSON.

### Transactional outbox and deferred execution

**Where:** `accounts/models.py::EmailVerificationCode` (`dispatch_token`, `queued_at`, `delivered_at`, `delivery_failed_at`), `accounts/services.py` (`publish_delivery`, `reconcile_pending_deliveries`, `deliver_verification_code`), `catalog/services.py::register_view_event`.

**Problem:** a database commit and a broker publish are two separate writes. A crash or a broker outage between them loses the letter; sending SMTP inside the request blocks a gunicorn thread for up to `EMAIL_TIMEOUT`. Side effects executed inside a transaction also survive its rollback.

**Solution:** the verification row is the outbox. Registration and resend issue a new dispatch token inside the transaction. `transaction.on_commit` makes one publish attempt and sets `queued_at`; Beat republishes rows that stayed unqueued for a minute. The worker sends only for the current token, an unexpired code and an undelivered dispatch, and derives the code from the stored nonce. Cache writes use the same commit hook.

**Trade-off:** delivery is at-least-once; a duplicate letter carries the same code. With the broker down, registration spends one failed publish attempt bounded by one-second connection and DNS timeouts, and the letter follows the next reconciliation after recovery.

### Concurrency control and locking

**Where:** `select_for_update` in `accounts/services.py` (`verify_email`, `resend_verification`, `purge_expired_registrations`), `comments/services.py::toggle_reaction` and `comments/signals.py`; PostgreSQL advisory lock in `catalog/services.py::_acquire_view_dedup_lock`.

**Problem:** attempt counters and resend limits are read, checked and written back. Without a lock, parallel requests read the same value and the limit does not trigger. Parallel reactions to one comment decide between add, switch and remove from a stale reaction. Parallel view events for the same viewer create duplicate rows.

**Solution:** pessimistic row locks for verification, resend and the reacted comment, an advisory lock keyed by title and viewer for view deduplication. A reaction and a user deletion both lock the user's row before any comment row. The shared order rules out a deadlock, and a reaction of a user being deleted waits for the deletion instead of landing between the recount and the cascade. The transactions are short and contended by a single user, so blocking is cheaper than optimistic retries.

### Denormalized counters and view history rotation

**Where:** `comments/models.py::Comment` (`likes_count`, `dislikes_count`), `catalog/models.py::AnimeDescription.total_views`, `comments/signals.py`, `catalog/services.py::purge_view_history`, `catalog/tasks.py`.

**Problem:** a comment page counted reactions for every comment of the title before `LIMIT`. With 100 000 comments and 400 000 reactions the first page took 113 ms: a parallel scan of all reactions and a sort on disk. The view count was `count(*)` over `ViewHistory`, so the history could not be trimmed and grew with every view.

**Solution:** the counters are columns updated with `F()` in the transaction that writes the reaction or the view. The page query became an index scan on `comments_page_idx` (`anime, -created_at, -id`) with `LIMIT`, 0.2 ms on the same data. Deleting a user removes reactions by cascade past the service, so a `pre_delete` handler locks the affected comments and decrements their counters in the same transaction. `ViewHistory` only serves the 24-hour deduplication window; Beat deletes older rows by primary key in batches. The migrations fill the counters from existing rows.

**Trade-off:** after rotation `total_views` cannot be recomputed from history. Reactions inserted past the service, for example by `bulk_create`, are repaired by `python manage.py recount_reactions` (`--anime <slug>` narrows it to one title). New views of one title wait for each other on the title row between the counter update and commit.

### Cache-aside with fail-open fallback

**Where:** `catalog/services.py`, `config/throttling.py`.

**Problem:** aggregate queries run on every page view, and a Redis outage must not take the site down. A read that computed the average before a vote commits can overwrite the fresh value.

**Solution:** a vote drops the cached average instead of writing the value it computed, otherwise a vote that read the aggregate earlier but finished later would leave a stale average for the whole TTL. Reads fill a missing key with `add` and never overwrite, and the TTL bounds how long a read that straddled a vote can stay. The title list expires by TTL. Every cache call goes through `_safe_cache` and falls back to PostgreSQL. Throttles use `FailOpenMixin` for regular endpoints and `FailClosedMixin` for authentication, each limit window with its own cache scope.

### Client-side facade

**Where:** `frontend/src/shared/api/client.ts`.

**Problem:** every call needs session cookies, the CSRF token for mutations, `204` handling, typed errors with `Retry-After` and cancellation on navigation.

**Solution:** a single `request` function handles all of it and returns typed data or throws `ApiError`. Query functions pass the React Query `AbortSignal`, so pending reads are aborted when a page unmounts; mutations are never aborted.

## Repository layout

```
.
├── backend/
│   ├── config/          # settings, urls, api root, Celery app, throttling
│   ├── accounts/        # auth, email verification, Celery tasks, profiles, IP lockout, management commands
│   ├── catalog/         # titles, ratings, view counter and history rotation, aggregate cache
│   ├── comments/        # comments, reactions, spam filter
│   ├── watch/           # watch progress
│   ├── loadtest/        # Locust scenario and runner script
│   └── Dockerfile       # python 3.14-slim, non-root; gunicorn, Celery worker and Beat
├── frontend/
│   ├── src/
│   │   ├── app/         # router, layout, error boundary
│   │   ├── pages/       # route components
│   │   ├── features/    # player, comments, rating, watch, avatar crop
│   │   ├── entities/    # anime: bundle metadata joined with API stats
│   │   └── shared/      # api client and generated types, session, ui kit
│   ├── nginx/           # server config and security-headers.conf
│   └── Dockerfile       # node build stage -> nginx
├── scripts/             # projectctl: setup and stack control
├── compose.yaml         # main stack
├── compose.dev.yaml     # dev override: vite HMR and runserver with mounted code
├── compose.demo.yaml    # demo override: frontend on loopback only
├── compose.loadtest.yaml
└── .env.example
```

## Getting started

### Environment

Copy `.env.example` to `.env` and fill it in. Generate secrets with:

```
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

With `DEBUG=False` startup requires `SECRET_KEY` and `EMAIL_DELIVERY_QUOTA_SECRET`. The second key protects the per-address send limit and is not rotated together with `SECRET_KEY`. With `DEBUG=True` and no key, a random key is written once to `backend/.dev-secret-key` (git-ignored, `0600` on Unix).

Secrets must not contain `$`: `docker compose` interpolates it.

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret key, required when `DEBUG=False` |
| `EMAIL_DELIVERY_QUOTA_SECRET` | HMAC key for the per-address send limit, required when `DEBUG=False` |
| `DEBUG` | `True`/`False`, default `False` |
| `ALLOWED_HOSTS` | Comma-separated hosts |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins |
| `APP_PORT` | Loopback port for the frontend, default `4173` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database connection |
| `REDIS_URL` | Cache, throttling and lockout; set by compose, in-process memory without it |
| `CELERY_BROKER_URL` | Celery broker; set by compose, tasks run inline in the Django process without it |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | SMTP credentials (Gmail App Password) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_SSL`, `EMAIL_TIMEOUT` | SMTP transport, default Gmail over SSL, 10 second timeout |
| `HTTPS_ENABLED` | https redirect, Secure cookies and HSTS, required in production |
| `API_DOCS_ENABLED` | Serve `/api/docs` and `/api/openapi.json`, default `DEBUG` |
| `NINJA_NUM_PROXIES` | Trusted proxy hops: `1` in demo, `2` in production |
| `SESSION_COOKIE_AGE` | Session lifetime in seconds, default 14 days |
| `LOG_TO_FILES` | Write log files to `backend/logs/`, default `True`; the Docker image sets `False` |
| `API_AUTH_THROTTLE`, `API_AUTH_THROTTLE_SUSTAINED`, `API_RESEND_THROTTLE`, `API_WRITE_THROTTLE`, `API_WRITE_THROTTLE_SUSTAINED`, `API_VIEW_THROTTLE`, `API_VIEW_THROTTLE_SUSTAINED`, `API_PROGRESS_THROTTLE`, `API_PROGRESS_THROTTLE_SUSTAINED` | Rate limit overrides |

### Docker

`scripts/projectctl.py` generates `.env`, validates the host and starts the stack under the fixed Compose project name `steinsgate_mailor`.

```
python scripts/projectctl.py init
# set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in .env
python scripts/projectctl.py validate
python scripts/projectctl.py up
```

`migrate` applies migrations and seeds the titles before `backend`, `celery-worker` and `celery-beat` start; `collectstatic` runs on backend start. `up` returns after HTTP 200 from the API, a healthy worker and a running Beat. The site is served at `http://localhost:4173`. Modes and commands: [scripts/README.md](scripts/README.md).

Admin access at `/admin/` is granted to an existing verified account, run from the repository root:

```
docker compose -p steinsgate_mailor exec backend python manage.py shell -c "from django.contrib.auth.models import User; print(User.objects.filter(username='okabe', is_active=True).update(is_staff=True, is_superuser=True))"
```

`1` means the account got access, `0` means there is no active account with that name. The account signs in on the site and then opens `/admin/`, everyone else lands on `/steins-gate`.

### Production proxy

Compose publishes the frontend on `127.0.0.1` only. A public deployment uses the host TLS proxy:

```nginx
location / {
    proxy_pass http://127.0.0.1:4173;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}

add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

The proxy owns the certificate and redirects HTTP to HTTPS. `X-Forwarded-For` is set from `$remote_addr`, not appended, so a client cannot supply its own value.

### Hot reload

Local development only, bypasses projectctl checks:

```
docker compose -f compose.yaml -f compose.dev.yaml up
```

Vite serves the SPA with HMR at `http://localhost:5173`, runserver reloads Django on code changes. The worker and Beat read the mounted code and pick up changes after `docker compose restart celery-worker celery-beat`.

### Without Docker

Backend: `cd backend`, create a venv, `pip install -r requirements.txt`, `python manage.py migrate`, `python manage.py runserver`. Frontend: `cd frontend`, `npm install`, `npm run dev`; Vite proxies `/api` and `/media` to `127.0.0.1:8000`.

Without `CELERY_BROKER_URL` tasks run inline, so the verification letter is sent during the request. Periodic cleanup is available as `python manage.py purge_expired_registrations` (`--dry-run` reports one batch) and `python manage.py clearsessions`. View history is trimmed only by Beat; by hand: `python manage.py shell -c "from catalog.tasks import purge_view_history; purge_view_history()"`.

## Testing

```
cd backend
python manage.py test
ruff check .

cd frontend
npm run lint
npm run build        # type check and production build
```

Backend tests run Celery in eager mode: the task executes in the commit hook, retries run synchronously. Broker failures, serialization of task arguments and retry classification have dedicated tests.

Production settings check:

```
cd backend
DEBUG=False SECRET_KEY=... EMAIL_DELIVERY_QUOTA_SECRET=... ALLOWED_HOSTS=example.com python manage.py check --deploy --fail-level WARNING
```

CI runs on pushes to `main` and `dev` and on pull requests: backend tests, deploy check, frontend lint and build, `nginx -t` on the production config, Gitleaks over the full history, projectctl tests. The projectctl job runs a full `up -> health check -> down` cycle under a temporary Compose project and checks the worker and Beat.

## Load testing

`backend/loadtest/run_loadtest.sh` runs Locust against an isolated Compose project. Configuration, baseline and cleanup: [backend/loadtest/README.md](backend/loadtest/README.md).

## Backlog

- Content Security Policy for Django admin pages.

## Author

- [mailor](https://github.com/mailorq) - fullstack
