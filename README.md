# Steins;Gate Fan Platform

Fan site for Steins;Gate: watch both seasons, the special episode and the movie, register, rate titles, keep watch progress and discuss episodes in comments.

It is a portfolio project: React SPA on the frontend, Django + django-ninja API with a service layer on the backend, PostgreSQL and Redis, everything in Docker behind nginx.

The current scope is finished. The next iteration moves verification mail and periodic cleanup to Celery, see [Roadmap](#roadmap).

## Architecture

```
browser
   |
   v
nginx (frontend container, :4173)
   |                security headers + CSP, rate limit on /admin/
   |-- /            SPA; index.html is revalidated so a new deploy is picked up
   |-- /assets/     hashed bundles, cached as immutable
   |-- /img/        posters and backgrounds (WebP)
   |-- /static/     Django admin static (shared volume)
   |-- /media/      user uploads (shared volume)
   |-- /api/   -----> backend (gunicorn, :8000)
   |-- /admin/ -----> backend (gunicorn, :8000)
                          |            |
                          v            v
                    PostgreSQL 16   Redis 7
                                    (throttling, IP lockout,
                                     aggregate cache)
```

- `frontend/` - React SPA split into `app -> pages -> features -> entities -> shared`. API types are generated from the backend OpenAPI schema.
- `backend/` - Django apps with a service layer; endpoints only validate input and call services.
- `compose.yaml` - `db`, `redis`, `backend`, `frontend`.

Title metadata (name, description, posters, player links) lives in the frontend bundle, and the API only returns what changes at runtime. Details are in [Design notes](#design-notes).

## Tech stack

| Layer    | Technology |
|----------|------------|
| Frontend | React 19, TypeScript (strict), Vite, Tailwind CSS 4, React Router 7, TanStack Query |
| Backend  | Python 3.14, Django 6, django-ninja, gunicorn |
| Storage  | PostgreSQL 16 (SQLite for local development), Redis 7 |
| Infra    | Docker, docker compose, nginx |
| Quality  | ruff, ESLint, Django tests, projectctl integration tests, GitHub Actions, Gitleaks |

## API

Swagger UI is at `/api/docs`, the schema at `/api/openapi.json` (both only when `API_DOCS_ENABLED` is on).

| Method    | Path | Auth |
|-----------|------|------|
| GET       | `/api/anime` | public |
| GET       | `/api/anime/{slug}` | public, rating and view counters only |
| POST      | `/api/anime/{slug}/view` | public, CSRF |
| POST      | `/api/anime/{slug}/rating` | session |
| GET, POST | `/api/anime/{slug}/comments` | POST: session |
| POST      | `/api/comments/{id}/reaction` | session |
| GET, PUT  | `/api/anime/{slug}/progress` | session |
| POST      | `/api/auth/register`, `/resend-verification`, `/verify-email`, `/login`, `/logout` | CSRF |
| GET       | `/api/auth/session`, `/api/auth/csrf` | public |
| PATCH     | `/api/profile`; POST `/api/profile/avatar` | session |

After changing a schema run `npm run gen:api` in `frontend/`.

## Security

### Accounts

- Session auth with HttpOnly cookies. django-ninja checks CSRF only inside cookie auth, so `register`, `login`, `verify-email`, `resend-verification` and `view` call `check_csrf` themselves.
- A new account stays inactive until the 6-digit code from the email is entered. The code lives 15 minutes and allows 5 attempts. The database keeps an HMAC hash and a nonce, not the code itself.
- Mail is sent after the registration transaction commits. If SMTP fails or times out, the account is kept, because the letter may still have been delivered, and the API answers `202`. The user can request the code again; a code that is still valid is resent as is.
- An expired unconfirmed registration frees its username and email, so nobody can hold someone else's address. Disabled accounts are not touched.
- One address can receive at most 6 letters per hour, counting both new registrations and resends. The counter is keyed by an HMAC of the address.
- Changing `SECRET_KEY` invalidates pending codes and sessions. Users can request a new code.
- `auth_user.email` has a partial case-insensitive unique index, so two parallel sign-ups cannot get the same address.

### Abuse control

- The client IP is taken from the right side of `X-Forwarded-For`, the part added by our own proxies. What the client puts there is ignored. Demo has one trusted hop, production two (host TLS proxy and nginx in compose).
- Auth endpoints, writes (ratings, comments, reactions, profile) and view events have a per-minute and a per-hour limit, resend has one hourly limit. Counters are in Redis and shared by all gunicorn workers. Reads and watch progress are not limited.
- Login and code entry have IP lockout: 5 failures give 30 seconds, every fourth series gives 10 minutes, success resets it.
- Django admin login is not covered by that lockout, so nginx limits `/admin/` itself.
- If Redis is down, writes and view events keep working without limits. Registration, login, code check and resend return `503` instead, since there they would become unlimited. Attempt and resend limits in the database still apply.

### Input and uploads

- Every route has an explicit Pydantic schema, nothing is bound from models directly. Comments go through a link and profanity filter.
- Avatars are checked by opening the image with Pillow, with size and pixel limits. The old file is deleted on replacement.

### Configuration and perimeter

- `DEBUG` is `False` by default. Without `SECRET_KEY` the app refuses to start instead of using some default key.
- `HTTPS_ENABLED` turns on the https redirect, Secure cookies and HSTS together. In production the app listens on loopback only, and the host TLS proxy is the only entry point.
- `EMAIL_TIMEOUT` limits how long a request waits for SMTP. A successful send means the mail server accepted the letter, not that it reached the inbox.
- Containers run with `no-new-privileges`. Backend and nginx drop all capabilities and run as non-root.
- nginx adds `nosniff`, `Referrer-Policy`, `X-Frame-Options` and `Permissions-Policy`, a CSP for the SPA and `default-src 'none'; sandbox` for uploaded files. `/api/` and `/admin/` get their headers from Django's `SecurityMiddleware`.

## Caching and logging

Redis caches the average rating (dropped on a new vote), view counters and the title list (both by TTL). A view is counted by a separate CSRF-protected `POST`. Whether a view is new within 24 hours is decided in PostgreSQL under an advisory lock, and Redis keeps a marker until that window ends so repeated requests skip the database. Nothing user-specific is cached.

Django writes logs to `backend/logs/`: `application.log` (domain events), `security.log` (lockouts, CSRF, spam) and `error.log` (errors from all loggers). `access.log` is only written by `runserver`. In Docker, gunicorn access and error logs go to container stdout.

## Design notes

### Static metadata in the bundle

The site has four fixed titles. Name, season, type, genres, description, poster and player links are in `frontend/src/shared/config/animes.ts`. Rating, view count, the user's own rating, progress and comments come from the API. That is why `GET /api/anime/{slug}` returns only `slug`, `avg_rating`, `total_views` and `user_rating` (`catalog/schemas.py::AnimeStatsOut`).

Keeping the text in both places would give two copies that drift apart: the page renders the config, so an edit in the database would not show up anywhere.

For a catalog with an admin UI the metadata should be in the database. Here titles do not change between releases and nobody edits them, so keeping them in the bundle saves a request before the first render. The price is that editing a description needs a frontend deploy. The database rows are still needed: slug uniqueness and foreign keys for ratings, views and comments point to them.

### Backend

**Service layer** - `backend/{accounts,catalog,comments,watch}/services.py`. Endpoints parse input, call a service and turn exceptions into status codes. Management commands (`purge_expired_registrations`, `seed_loadtest`) and most tests call the same functions without HTTP.

**Schemas as the contract** - `backend/*/schemas.py`. Adding a model field does not change the API by accident. Limits are also set there. For example `watch/schemas.py::ProgressIn` rejects `Infinity`: `json.loads` accepts it, and a saved infinity comes back as `NaN`, which is not valid JSON.

**Registration result** - `accounts/services.py::RegistrationResult`. Registration can end with "account created, letter sent" or "account created, sending not confirmed". Raising an exception for the second case would roll back the transaction and delete an account whose letter may have arrived. The dataclass returns both facts and the API maps them to `201` and `202`.

**Side effects after commit** - `accounts/services.py::_dispatch_code_delivery`, `catalog/services.py::register_view_event`. Mail is sent and cache is written only after the transaction commits. Otherwise a rollback could leave a letter pointing to a missing row, or a cache entry for a row that was never saved.

**Row locks** - `select_for_update` in `verify_email`, `resend_verification`, `purge_expired_registrations`, and a PostgreSQL advisory lock in `catalog/services.py::_acquire_view_dedup_lock`. Verification reads the attempt counter, decides and writes it back. Without a lock two parallel requests both read the old value and the limit never triggers. These transactions are short and usually hit by one user, so waiting on a lock is cheaper than retrying on a version conflict.

**Cache with explicit invalidation** - `catalog/services.py`. The average rating is dropped on a new vote because the user who just voted would see a stale number. View counters and the title list expire by TTL, a minute of delay there does not matter. Every cache call goes through `_safe_cache`, so a Redis failure falls back to the database.

**Different failure policy for throttles** - `config/throttling.py`. `FailOpenMixin` and `FailClosedMixin` wrap the same django-ninja classes. Each limit window has its own scope, otherwise two windows would count the same requests.

**CSRF on routes without cookie auth** - `accounts/api.py::csrf_rejected`, `catalog/api.py::register_view`. django-ninja marks all views `csrf_exempt` and only checks CSRF inside cookie auth, so these routes check it explicitly.

**Narrow title lookup** - `catalog/models.py::AnimeDescription.refs()`. View, rating, comment and progress endpoints only need `id` and `slug`, so they do not load the description text and poster path.

### Frontend

**Layers** - `frontend/src/{app,pages,features,entities,shared}`. Imports go only downward. `shared` knows nothing about the domain, so the API client and UI kit have no Steins;Gate specifics.

**Title entity** - `frontend/src/entities/anime/model.ts`. `useAnime` joins the config entry and the API stats by slug. The page passes the stats down to the rating widget, and `useRateAnime` writes the vote result back into the same query key through `animeStatsKey`. The key is built in one place so the write and the read cannot end up under different keys.

**API client** - `frontend/src/shared/api/client.ts`. One `request` function handles cookies, fetching the CSRF token, `204` responses, `Retry-After` and request cancellation through `AbortSignal`. Queries pass the React Query signal, so leaving a page aborts its pending reads.

**Error boundary** - `frontend/src/app/AppErrorBoundary.tsx` wraps the providers and the router and shows a reload screen instead of a blank page. Errors inside the player iframes are not React errors and never reach it.

### Tooling

`scripts/projectctl.py` refuses to start the stack on a busy port, `DEBUG=True` in production, `ALLOWED_HOSTS=*`, a `$` inside a secret (Compose would change the value) or Docker resources that it cannot link to this checkout. If `docker compose config` returns something it cannot parse, that is an error too.

## Repository layout

```
.
├── backend/
│   ├── config/          # settings, urls, api root, throttling, logging
│   ├── accounts/        # auth, email verification, profiles, IP lockout, management commands
│   ├── catalog/         # titles, ratings, view history, aggregate cache
│   ├── comments/        # comments, reactions, spam filter
│   ├── watch/           # watch progress
│   ├── loadtest/        # Locust scenario and runner script
│   └── Dockerfile       # python 3.14-slim, non-root, gunicorn
├── frontend/
│   ├── src/
│   │   ├── app/         # router, layout, error boundary
│   │   ├── pages/       # route components
│   │   ├── features/    # player, comments, rating, watch, avatar crop
│   │   ├── entities/    # anime: config entry joined with API stats
│   │   └── shared/      # api client and generated types, session, ui kit
│   ├── nginx/           # server config and security-headers.conf
│   └── Dockerfile       # node build stage -> nginx
├── scripts/             # projectctl: setup and stack control
├── compose.yaml         # main stack
├── compose.dev.yaml     # dev override: vite HMR and runserver with mounted code
├── compose.demo.yaml    # demo override: frontend on loopback only
└── .env.example
```

## Getting started

### Environment

Copy `.env.example` to `.env` in the repository root and fill it in. A secret key can be generated with:

```
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

With `DEBUG=False` the app does not start without `SECRET_KEY` and `EMAIL_DELIVERY_QUOTA_SECRET`. The second one is the HMAC key for the per-address send limit and should not change when `SECRET_KEY` is rotated. With `DEBUG=True` and no key, a random one is written once to `backend/.dev-secret-key` (git-ignored, `0600` on Unix).

Do not use `$` in secrets: `docker compose` treats it as a variable and the container gets a different value.

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret key, required when `DEBUG=False` |
| `EMAIL_DELIVERY_QUOTA_SECRET` | HMAC key for the per-address send limit, required when `DEBUG=False`, keep it when rotating `SECRET_KEY` |
| `DEBUG` | `True`/`False`, default `False`. Mail is sent in both modes |
| `ALLOWED_HOSTS` | Comma-separated hosts |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins |
| `APP_PORT` | Loopback port for the frontend, default `4173` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database connection |
| `REDIS_URL` | Set by compose. Without it Django uses in-process memory |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | Gmail SMTP login, needs an App Password |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_SSL`, `EMAIL_TIMEOUT` | SMTP transport, defaults are Gmail over SSL with a 10 second timeout |
| `HTTPS_ENABLED` | https redirect, Secure cookies and HSTS, required in production |
| `API_DOCS_ENABLED` | Serve `/api/docs` and the schema, defaults to `DEBUG` |
| `NINJA_NUM_PROXIES` | Trusted proxy hops: `1` in demo, `2` in production |
| `SESSION_COOKIE_AGE` | Session lifetime in seconds, default 14 days |
| `API_AUTH_THROTTLE`, `API_AUTH_THROTTLE_SUSTAINED`, `API_RESEND_THROTTLE`, `API_WRITE_THROTTLE`, `API_WRITE_THROTTLE_SUSTAINED`, `API_VIEW_THROTTLE`, `API_VIEW_THROTTLE_SUSTAINED` | Rate limit overrides |

If migration `0011_emaildeliveryquota` was deployed before `EMAIL_DELIVERY_QUOTA_SECRET` existed, set it to the current `SECRET_KEY` on the first deploy and do not change it afterwards, otherwise the running hourly limits reset. `projectctl.py init` does this for an existing `.env`.

### Docker

`scripts/projectctl.py` creates `.env`, checks the machine and starts the stack under a fixed Compose project name, so other projects on the host are not affected. It waits for HTTP 200 from the API before reporting success.

```
python scripts/projectctl.py init
# fill EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in .env
python scripts/projectctl.py validate
python scripts/projectctl.py up
```

The site is then at `http://localhost:4173`. Migrations, title seeding and `collectstatic` run when the backend starts. Modes and all commands are described in [scripts/README.md](scripts/README.md).

### Production proxy

Compose publishes the port on `127.0.0.1` only. For a public deployment put the host TLS proxy in front of it instead of binding to `0.0.0.0`:

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

The proxy holds the certificate and redirects HTTP to HTTPS. `X-Forwarded-For` has to be set from `$remote_addr`, not appended to, otherwise a client can send its own value.

### Hot reload

On a local machine only, since this skips the projectctl checks:

```
docker compose -f compose.yaml -f compose.dev.yaml up
```

Code is mounted from the host. Vite serves the SPA with HMR at `http://localhost:5173`, Django restarts on changes.

### Without Docker

Backend: `cd backend`, create a venv, `pip install -r requirements.txt`, `python manage.py migrate`, `python manage.py runserver`. Frontend: `cd frontend`, `npm install`, `npm run dev`. Vite proxies `/api` and `/media` to `127.0.0.1:8000`.

Expired registrations and old send limits are removed by `python manage.py purge_expired_registrations`, run it daily from cron for now. It only deletes inactive users with an expired code. `--dry-run` shows what one batch would delete.

## Testing

```
cd backend
python manage.py test
ruff check .

cd frontend
npm run lint
npm run build        # type check and production build
```

The production settings are checked separately, same as in CI:

```
cd backend
DEBUG=False SECRET_KEY=... EMAIL_DELIVERY_QUOTA_SECRET=... ALLOWED_HOSTS=example.com python manage.py check --deploy --fail-level WARNING
```

CI runs on pushes to `main` and `dev` and on pull requests. Jobs: backend tests, the deploy check above, frontend lint and build, `nginx -t` on the real config, Gitleaks over the whole history, and the projectctl tests. The projectctl job starts the stack under a temporary Compose project, waits for the health check and shuts it down, without touching existing containers or volumes.

## Load testing

`backend/loadtest/run_loadtest.sh` runs Locust against a separate Compose project with its own volumes and port. It refuses non-local targets unless allowed explicitly. On the development machine the stack held about 234 RPS at 500 concurrent users with p95 around 1.3 seconds. 1000 users is above what the demo setup is meant for.

The load test uses the `locmem` mail backend, so it does not show the cost of real SMTP. Safeguards, results and cleanup are in [backend/loadtest/README.md](backend/loadtest/README.md).

## Roadmap

- [x] SPA with generated API types, session auth, comments, ratings, watch progress
- [x] django-ninja API over a service layer
- [x] Redis throttling, IP lockout and aggregate cache
- [x] Email verification: hashed codes, SMTP failure handling, resend with a per-address limit, cleanup of expired registrations
- [x] Security pass: client IP trust, CSRF on routes without cookie auth, upload checks, CSP and edge headers, secret scanning in CI
- [x] Production perimeter: loopback-only port and documented TLS proxy setup
- [x] CI: tests, deploy check, nginx config, Gitleaks, projectctl lifecycle test, Locust baseline
- [x] Stats-only title endpoint, indexes for view deduplication, cleaned Docker build context
- [ ] Celery worker for verification mail and Celery Beat for cleanup. The broker gets its own Redis instance: memory limit and eviction policy in Redis apply to the whole instance, so a separate logical database would not protect the lockout and throttle keys from a growing queue.

Not planned: a database-driven catalog with an admin UI, and scaling past the measured demo load.

## Author

- [mailor](https://github.com/mailorq) - fullstack
