# Steins;Gate Fan Platform

A single-title streaming-style web application dedicated to Steins;Gate: watch both seasons and the movie, register an account, manage a profile, rate titles and discuss them in comments.

The project is a portfolio work demonstrating a production-shaped full-stack setup: a typed SPA frontend, a Django API backend with a service layer, and a containerized deployment behind nginx with Redis-backed rate limiting and caching.

**Project status: the current scope is complete.** It is intentionally fixed to
a small, polished Steins;Gate platform rather than an open-ended streaming
service. Security checks, deployment safeguards, CI and a measured load-testing
baseline are part of the finished scope, not future work. The next planned
iteration moves email delivery onto a background queue; the seam it will use is
described under
[deferred side effect](#architecture-decisions-and-design-patterns).

## Architecture

```
browser
   |
   v
nginx (frontend container, :4173)
   |                security headers + CSP, rate limit on /admin/
   |-- /            SPA static; index.html revalidated so a deploy is picked up
   |-- /assets/     hashed bundles, cached immutable
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

- `frontend/` — React SPA, layered `app → pages → features → entities → shared`. API types are generated from the backend OpenAPI schema, so the contract is compile-checked.
- `backend/` — Django + django-ninja. Domain apps with a service layer; HTTP endpoints are thin wrappers over services.
- `compose.yaml` — four services: `db`, `redis`, `backend`, `frontend`.

The catalog uses a **hybrid data model**: immutable title metadata ships inside
the frontend bundle, while everything that changes at runtime is served by the
API. [Architecture decisions](#architecture-decisions-and-design-patterns)
explains the split and the rest of the reasoning behind the code layout.

## Tech stack

| Layer      | Technology |
|------------|------------|
| Frontend   | React 19, TypeScript (strict), Vite, Tailwind CSS 4, React Router 7, TanStack Query |
| Backend    | Python 3.14, Django 6, django-ninja, gunicorn |
| Storage    | PostgreSQL 16 (SQLite for local development), Redis 7 |
| Infra      | Docker, docker compose, nginx |
| Quality    | ruff, ESLint, Django tests, projectctl integration tests, GitHub Actions CI, Gitleaks |

## API

Interactive documentation: `/api/docs` (OpenAPI schema at `/api/openapi.json`).

| Method     | Path | Auth |
|------------|-------------------------------|---------|
| GET        | `/api/anime`                  | public  |
| GET        | `/api/anime/{slug}`           | public; dynamic stats only |
| POST       | `/api/anime/{slug}/view`      | public, CSRF |
| POST       | `/api/anime/{slug}/rating`    | session |
| GET, POST  | `/api/anime/{slug}/comments`  | POST: session |
| POST       | `/api/comments/{id}/reaction` | session |
| GET, PUT   | `/api/anime/{slug}/progress`  | session |
| POST       | `/api/auth/register`, `/resend-verification`, `/verify-email`, `/login`, `/logout` | — |
| GET        | `/api/auth/session`, `/api/auth/csrf` | public |
| PATCH      | `/api/profile`; POST `/api/profile/avatar` | session |

Frontend types are regenerated with `npm run gen:api` after the schema changes.

## Security

**Authentication and sessions**

- Session authentication with HttpOnly cookies. django-ninja enforces CSRF inside cookie auth, so every session-protected mutation is covered automatically; `register`, `login` and `verify-email` carry no cookie auth and therefore call `check_csrf` explicitly.
- Registration creates an inactive user; the account activates only after a 6-digit email code (15-minute TTL, attempt limit, constant-time comparison). The database stores an HMAC hash and a nonce, never the plaintext code. SMTP is dispatched only after the registration transaction; a timeout keeps the pending account intact because the original letter may still have arrived. The client receives `202` when delivery cannot be confirmed and can use the CSRF-protected resend endpoint. A valid code is resent unchanged, so a failed resend cannot invalidate a code that already works.
- An unverified registration whose code has expired releases its username and address, so a third party cannot squat someone else's email. Disabled accounts are never touched by that cleanup. A recipient-scoped, HMAC-keyed quota allows at most six delivery attempts per hour across registration replacement and resend. Run `python manage.py purge_expired_registrations` periodically to remove old pending records and expired quota entries.
- Rotating `SECRET_KEY` intentionally invalidates pending verification codes (as well as Django sessions); affected users can request a fresh code.
- `User.email` is unique through a partial case-insensitive index, which closes the race between two concurrent sign-ups.

**Abuse control**

- Client IP is read from the trusted right-hand side of `X-Forwarded-For`, matching how django-ninja resolves it. Values a client prepends to the header are ignored, so IP lockout cannot be bypassed by spoofing. Production has two trusted hops (host TLS proxy and compose nginx); the host proxy must replace, not append, the client-supplied forwarding headers.
- Two-level rate limiting per endpoint group (burst + sustained window), counters shared across workers via Redis.
- IP lockout on credential and code entry: 5 consecutive failures block for 30 seconds, escalating series block for 10 minutes; a successful attempt resets the counter.
- The Django admin login is outside the application lockout, so nginx rate-limits `/admin/` at the edge.
- Read and ordinary write throttles degrade fail-open so a transient cache outage does not take the site down. Registration, login, email-code verification and resend are different: if their shared throttle storage is unavailable, they fail closed with a controlled `503`; database attempt/resend limits remain a second line of defence.

**Input and uploads**

- Comment spam filter, request body limits, explicit Pydantic schemas on every route (no auto-binding, so mass assignment is not possible).
- Avatars are validated by decoding the image, not by trusting the extension; the previous file is deleted on replacement so repeated uploads cannot fill the disk.

**Configuration and perimeter**

- `DEBUG` defaults to `False`, and an empty `SECRET_KEY` with `DEBUG=False` aborts startup instead of silently falling back to a key from the repository.
- `HTTPS_ENABLED` switches the https redirect, Secure cookies and HSTS as one unit. Production requires it and binds the application to loopback only; a host TLS proxy is the sole public entry point and must overwrite the forwarding headers it receives from clients.
- A failing SMTP server returns a controlled `202` with a pending registration instead of an unhandled `500`; no user is deleted after an ambiguous timeout. `EMAIL_TIMEOUT` bounds how long a request can wait on the mail server. The result only confirms acceptance by the configured mail backend, not final inbox delivery.
- Both the docs UI and the schema itself are served only when `API_DOCS_ENABLED` is on (default: `DEBUG`) — hiding `/api/docs` alone would leave `/api/openapi.json` readable.
- Containers run with `no-new-privileges`; the backend and the nginx image drop all capabilities and run as non-root users.
- nginx sends `nosniff`, `Referrer-Policy`, `X-Frame-Options` and `Permissions-Policy` on everything it serves, a CSP for the SPA, and an isolating `default-src 'none'; sandbox` policy for user uploads. `server_tokens` is off. Headers are set only where nginx serves the response — `/api/` and `/admin/` keep the ones Django's `SecurityMiddleware` produces, so nothing is duplicated.

## Caching and logging

Redis caches hot aggregates: average rating (invalidated on new votes), view counters and the title list (TTL). A title-view event is an explicit CSRF-protected `POST`; Redis provides only a short cross-worker mutex, while the database remains the source of truth for the 24-hour deduplication window. Personalized data is never cached.

Logs are split by purpose in `backend/logs/` (rotating files): `access.log` (HTTP), `application.log` (domain events), `security.log` (lockouts, CSRF, spam), `error.log` (errors only), `worker.log` (gunicorn lifecycle).

## Architecture decisions and design patterns

This section records *why* the code has the shape it has. Every entry names the
files it lives in, the problem it solves, and the naive alternative that was
rejected. Patterns are listed only where they are actually load-bearing.

### Hybrid data model: static metadata in the bundle, dynamics through the API

The catalog is four fixed titles that change only with a release. Their name,
season, type, genres, description, poster and player sources live in
`frontend/src/shared/config/animes.ts` and ship inside the JS bundle. Everything
that changes at runtime — average rating, view counter, the visitor's own rating,
watch progress, comments — comes from the API. `GET /api/anime/{slug}` therefore
returns four fields (`catalog/schemas.py::AnimeStatsOut`): the `slug` that
identifies the record, plus the three counters.

**Problem it solves.** Before the split the same description existed twice: as a
row in `catalog_animedescription` and as a literal in the frontend config. The
page rendered the config copy and discarded the API copy, so a description could
be edited in the database and change nothing on screen — a silent divergence that
no test could catch, because both copies were individually correct.

**Why not the naive approach.** Serving metadata from the database is the
textbook answer, and it is the right one for a real catalog with an editor UI.
This project has neither: the titles are fixed, there is no admin workflow for
them, and the text is part of the design. Keeping it server-side would ship a
`TextField` over the wire on every page view and put a network round-trip in
front of the first paint. Keeping it in the bundle renders the page immediately
and turns content edits into reviewable diffs.

**Cost, stated honestly.** Metadata changes now require a frontend deploy, and
the database rows remain the catalog's identity — slug uniqueness and the foreign
keys for ratings, views and comments still live there. That trade is written down
so the next person does not "fix" it by accident.

The join happens in exactly one place: `frontend/src/entities/anime/model.ts`.

### Backend patterns

**Service layer** — `backend/{accounts,catalog,comments,watch}/services.py`.
API modules validate input, call one service function and map exceptions to
status codes; the rules live in services. This keeps the domain callable without
HTTP: `purge_expired_registrations` and `seed_loadtest` are management commands
that invoke the same functions the API does, and most tests exercise services
directly. Rules written into the view would force every one of those callers
through a synthetic request.

**DTO / schema-driven validation** — `backend/*/schemas.py`. Every route declares
an explicit Pydantic schema; nothing is auto-bound from a model. Adding a model
field therefore cannot silently widen the API, and mass assignment is impossible
by construction. Bounds live there too: `watch/schemas.py::ProgressIn` rejects
non-finite numbers, because `json.loads` accepts the non-standard `Infinity`
literal and a stored infinity comes back as `NaN` — a response that is no longer
valid JSON for any client.

**Result object** — `accounts/services.py::RegistrationResult`. Registration has
two successful outcomes: the account exists and the code was delivered, or the
account exists and delivery could not be confirmed. Signalling the second with an
exception would roll the transaction back and destroy an account whose
verification letter may well have arrived. A frozen dataclass carries both facts
out of the service, and the API maps them to `201` and `202`.

**Deferred side effect / commit hook** — `accounts/services.py::_dispatch_code_delivery`
and `catalog/services.py::register_view_event`. Mail is dispatched after the
transaction commits, and cache writes are registered through
`transaction.on_commit`. A letter sent inside a transaction can advertise a row
that a rollback then removes; a cache entry written inside one can outlive the
row it describes. This helper is also the seam where a broker-backed queue
replaces the synchronous send without touching the API contract.

**Pessimistic locking** — `select_for_update` in `accounts/services.py`
(`verify_email`, `resend_verification`, `purge_expired_registrations`) and a
PostgreSQL advisory lock in `catalog/services.py::_acquire_view_dedup_lock`.
Verification and resend read a counter, decide, then write it back; without a
lock two concurrent requests both read the old value and the attempt limit never
trips. Version columns with retries would also work, but these paths are short
and contended by a single user, so blocking is cheaper than retrying.

**Cache-aside with explicit invalidation** — `catalog/services.py`. Average
rating is read through the cache and invalidated on a new vote; view counters and
the title list expire by TTL, because a minute of lag on a counter is invisible
while a stale rating is noticed immediately by the user who just voted. Every
cache call goes through `_safe_cache`, so a Redis outage degrades to database
reads rather than an error page. Personalised data is never cached.

**Mixin-composed failure policy** — `config/throttling.py`. `FailOpenMixin` and
`FailClosedMixin` wrap the same django-ninja throttle classes with opposite
behaviour when the counter store is unreachable. Reading a page has to survive a
Redis blip; registration and login must not silently become unlimited, so they
return a controlled `503`. Each window is its own class with its own scope — two
instances sharing a scope would count the same requests twice.

**Guard clause for an inverted default** — `accounts/api.py::csrf_rejected` and
`catalog/api.py::register_view`. django-ninja marks every view `csrf_exempt` and
re-enables CSRF only inside cookie authentication, so a route without cookie auth
is unprotected by default. One named helper makes the check explicit and
greppable instead of leaving it to a framework detail that reads backwards.

**Narrowed query factory** — `catalog/models.py::AnimeDescription.refs()`. The
view, rating, comment and progress endpoints need only a primary key and a slug.
`refs()` is the single definition of that projection, so no hot path pulls a
`TextField` and a poster path out of the database only to discard them.

**Registry** — `config/api.py`. One `NinjaAPI` instance owns the routers, the
throttling exception handler and the schema gate. `docs_url` and `openapi_url`
are switched by the same flag, because hiding the docs UI while leaving the
schema readable would publish the full endpoint map anyway.

**Command** — `accounts/management/commands/purge_expired_registrations.py`.
Retention is a scheduled operation, not a request. As a management command it
runs from cron, supports `--dry-run`, and needs no HTTP surface to secure.

### Frontend patterns

**Feature-Sliced layering** — `frontend/src/{app,pages,features,entities,shared}`.
Dependencies point one way: downward. `shared` knows nothing about the domain,
`entities` owns a domain object, `features` owns an interaction, `pages` compose
them. That rule is what keeps the API client free of Steins;Gate specifics and
lets the rating widget move without dragging its page along.

**Adapter / selector hook** — `frontend/src/entities/anime/model.ts::useAnime`.
The single place where the static config and the API response are joined by
`slug`; callers get one object and never learn it came from two sources. Joining
inside the page would repeat the logic in every consumer and let the copies
drift — the exact duplication the hybrid model exists to remove.

**Single owner of cache keys** — `animeStatsKey`, in the same module. The page
and the rating widget request the same key, so React Query issues one network
call and both render from one cache entry; `useRateAnime` writes the mutation
result back through that same key. While keys were assembled at each call site, a
write could land beside the read instead of on top of it.

**Custom hooks as the unit of reuse** — `entities/anime/model.ts`,
`features/watch/useWatchProgress.ts`, `shared/session/sessionContext.ts`.
Stateful logic — polling the player, debouncing progress saves, reading the
session — lives in hooks, so components stay declarative.

**Context and provider, split across files** — `shared/session/sessionContext.ts`
holds the context and `useSession`; `shared/session/SessionProvider.tsx` holds
the component. The header, pages and features all need session state at once, and
threading it through props would touch every layer. The split keeps any single
file from exporting both a component and a hook, which is what keeps Fast Refresh
working while developing.

**Error boundary** — `frontend/src/app/AppErrorBoundary.tsx`, wrapping the query
provider, the session provider and the router. React offers no hook equivalent,
so this is deliberately the only class component in the codebase: a render error
anywhere beneath it produces a recovery screen instead of a blank page. The
component stack is logged in development only.

**Facade over `fetch`** — `frontend/src/shared/api/client.ts`. One function owns
credentials, the CSRF token round-trip, `204` handling and the translation of a
failed response into a typed `ApiError` carrying `Retry-After`. Components never
touch a raw `Response`, and rate-limit handling is written once.

**Generated types as the contract** — `frontend/src/shared/api/types.gen.ts`,
regenerated by `npm run gen:api`. The build type-checks the frontend against the
backend's OpenAPI schema, so a field removed server-side breaks `npm run build`
instead of surfacing as `undefined` in the browser.

### Operational tooling

**Fail-closed validation** — `scripts/projectctl.py`. The launcher refuses to
start on an occupied port, on `DEBUG=True` in production, on `ALLOWED_HOSTS=*`,
on a `$` inside a secret that Compose would silently mangle, and on Docker
resources it cannot prove belong to this checkout. An unparseable
`docker compose config` counts as a failure, never as "no problems found".

## Repository layout

```
.
├── backend/
│   ├── config/          # settings, urls, api root, throttling, logging
│   ├── accounts/        # auth, email verification, profiles, IP lockout
│   ├── catalog/         # titles, ratings, view history, aggregate cache
│   ├── comments/        # comments, reactions, spam filter
│   ├── watch/           # watch progress
│   ├── loadtest/        # isolated Locust scenario, seed and SQL-query profiler
│   └── Dockerfile       # python 3.14-slim, non-root, gunicorn
├── frontend/
│   ├── src/
│   │   ├── app/         # router, layout
│   │   ├── pages/       # route components
│   │   ├── features/    # player, comments, rating, watch, avatar crop
│   │   ├── entities/    # anime: static config joined with API stats
│   │   └── shared/      # api client + generated types, session, ui kit
│   ├── nginx/           # server config + shared security-headers.conf
│   └── Dockerfile       # node build stage -> nginx
├── scripts/             # projectctl: guided setup and stack control
├── compose.yaml         # production-shaped stack
├── compose.dev.yaml     # dev override: vite HMR + runserver, host-mounted code
├── compose.demo.yaml    # demo override: frontend published on loopback only
└── .env.example
```

## Getting started

### Environment

Copy `.env.example` to `.env` in the repository root and fill in the values. Generate the secret key with:

```
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

With `DEBUG=False`, empty `SECRET_KEY` or `EMAIL_DELIVERY_QUOTA_SECRET` stops the application on startup. The latter is a separate stable HMAC key for the recipient send budget and must not rotate with Django's session key. There is no placeholder key in the repository at all: under `DEBUG=True` a random `SECRET_KEY` is generated once into `backend/.dev-secret-key` (git-ignored, owner-only `0600` on Unix), so a forgotten `.env` can never fall back to a value an attacker already knows.

Keep the secret URL-safe. `docker compose` treats `$` as variable interpolation, so a `$` inside `SECRET_KEY` silently changes the value the container receives and breaks sessions and CSRF.

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret key; required whenever `DEBUG=False`, startup fails without it |
| `EMAIL_DELIVERY_QUOTA_SECRET` | Stable HMAC key for the per-recipient delivery budget; required whenever `DEBUG=False`; do not change it during normal `SECRET_KEY` rotation |
| `DEBUG` | `True`/`False`, defaults to `False`; controls Django diagnostics but does not disable email delivery |
| `ALLOWED_HOSTS` | Comma-separated host list |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated origins for production |
| `APP_PORT` | Loopback-only host port for the frontend; use a different value when another project uses `4173` |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database connection |
| `REDIS_URL` | Optional; set by compose in Docker, in-process memory is used without it |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | Gmail SMTP credentials (an App Password is required); used in every mode |
| `HTTPS_ENABLED` | Switches https redirect, Secure cookies and HSTS together; required in production, where a host TLS proxy handles the certificate and HSTS |
| `API_DOCS_ENABLED` | Serve `/api/docs` and the OpenAPI schema; defaults to `DEBUG` |
| `NINJA_NUM_PROXIES` | Trusted proxy hops in front of Django; `1` in demo, `2` in production (host TLS proxy plus compose nginx) |
| `SESSION_COOKIE_AGE` | Session lifetime in seconds (default 14 days) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_SSL`, `EMAIL_TIMEOUT` | SMTP transport; defaults target Gmail over SSL with a 10s timeout |
| `API_AUTH_THROTTLE`, `API_AUTH_THROTTLE_SUSTAINED`, `API_RESEND_THROTTLE`, `API_WRITE_THROTTLE`, `API_WRITE_THROTTLE_SUSTAINED`, `API_VIEW_THROTTLE`, `API_VIEW_THROTTLE_SUSTAINED` | Rate limit overrides |

If migration `0011_emaildeliveryquota` was already deployed before this separate setting existed, set `EMAIL_DELIVERY_QUOTA_SECRET` to the **current** `SECRET_KEY` for the first upgraded deploy, then keep it unchanged. This preserves the active one-hour quota without revealing either value; `projectctl.py init` performs this one-time compatibility step for an existing valid `.env`.

### Run with Docker safely

`scripts/projectctl.py` prepares `.env`, validates the host and brings the stack
up under a fixed Compose project name, so other Docker projects on the machine
are untouched. It generates secrets, refuses to start on a misconfigured
environment (occupied port, `DEBUG=True` on a public interface, a `$` inside a
secret that Compose would silently mangle) and waits for a real HTTP 200 before
reporting success.

```
python scripts/projectctl.py init
# Fill EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in .env.
python scripts/projectctl.py validate
python scripts/projectctl.py up
```

See [scripts/README.md](scripts/README.md) for modes, ownership checks and the
full command reference. The application is available at `http://localhost:4173`;
migrations, title seeding and `collectstatic` run automatically on backend start.

### Public production proxy

The Compose port is intentionally bound only to `127.0.0.1`. For a public
deployment, put it behind the host's existing TLS reverse proxy; do not change
the Compose binding to `0.0.0.0`. Inside the proxy's `listen 443 ssl` server,
the location should follow this trust boundary:

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

The proxy must own the certificate and redirect HTTP to HTTPS. Assigning
`X-Forwarded-For` from `$remote_addr` (rather than appending a client header)
prevents clients from forging their apparent address or scheme.

### Development mode with hot reload

For an isolated local workstation only (this direct Compose command deliberately
bypasses `projectctl` ownership and shared-host safety checks):

```
docker compose -f compose.yaml -f compose.dev.yaml up
```

Source directories are mounted from the host: vite serves the SPA with HMR at `http://localhost:5173`, Django restarts on backend changes. No image rebuilds needed while coding.

### Run locally without Docker

Backend: `cd backend`, create a venv, `pip install -r requirements.txt`, `python manage.py migrate`, `python manage.py runserver`. Frontend: `cd frontend`, `npm install`, `npm run dev` — vite proxies `/api` and `/media` to `127.0.0.1:8000`.

For a production-like scheduler, run `python manage.py purge_expired_registrations` daily. It deletes only inactive users with an expired verification record and expired delivery-quota entries; `--dry-run` previews one batch without changing data.

## Testing and linting

```
cd backend
python manage.py test        # service, API, lockout, cache and N+1 regression tests
ruff check .

cd frontend
npm run lint
npm run build                # strict type check + production build
```

The production configuration profile is verified separately, the same way CI does it:

```
cd backend
DEBUG=False SECRET_KEY=... EMAIL_DELIVERY_QUOTA_SECRET=... ALLOWED_HOSTS=example.com python manage.py check --deploy --fail-level WARNING
```

CI runs six jobs on every push to `main`/`dev` and on every pull request:
backend tests, the deployment checklist above, the frontend build, `nginx -t`
against the real perimeter config, a Gitleaks scan of the full history, and the
`projectctl` suite. The latter includes an isolated Docker
`up → health → down` lifecycle check. It uses a temporary Compose project and
does not touch a developer's existing containers or volumes.

## Load testing

The repository includes a repeatable, isolated Locust baseline rather than a
claim based on unmeasured performance. Its stack uses a separate Compose project,
temporary volumes and a loopback port, and refuses a non-local target unless
explicitly authorised. The recorded development-machine baseline sustained about
234 RPS at 500 concurrent users with p95 around 1.3 seconds and no meaningful
error rate; 1,000 simulated users exceed the intended demo capacity.

Run the complete local measurement with Docker Desktop:

```
backend/loadtest/run_loadtest.sh
```

See [backend/loadtest/README.md](backend/loadtest/README.md) for safeguards,
hardware-dependent results, reports and cleanup.

## Project roadmap

- [x] SPA frontend with generated API types, session auth, comments, ratings, watch progress.
- [x] django-ninja API over a service layer, domain app split.
- [x] Redis: two-level throttling, IP lockout, aggregate caching, fail-open degradation.
- [x] Structured logging, avatar cropping, responsive header, dev compose with HMR.
- [x] Email-verification flow: hashed codes, controlled SMTP failures, resend with a shared recipient quota and stale-registration cleanup.
- [x] Application security pass: client-IP trust model, CSRF on unauthenticated routes, upload validation, edge headers and CSP, secret scanning in CI.
- [x] Production perimeter: loopback-only application port and documented TLS reverse-proxy trust boundary.
- [x] Verification: backend/frontend lint and tests, deployment checks, secret scanning, `projectctl` lifecycle integration and an isolated Locust baseline.
- [x] Hybrid data model: the detail endpoint returns dynamic stats only, static metadata is joined into it by an entity-layer hook on the client.
- [x] Query and image hygiene: composite indexes for the view-deduplication paths, a narrowed projection for hot lookups, and a build context stripped of caches and test tooling.

Background mail delivery is the **next planned iteration**, not a non-goal:
`_dispatch_code_delivery` already isolates the send from the transaction, so a
worker slots in behind it without changing the `201`/`202` contract. Note that
Redis currently serves the cache, throttle counters and IP lockout on database
`0`; a broker must be given its own database index so that clearing a queue
cannot wipe the lockout state.

The following remain deliberate **non-goals**, not unfinished defects: a general
CMS/database catalog for more titles, and scaling beyond the measured demo
profile. They would change the product scope and should be designed as a separate
iteration if the project grows.

## Author

- [mailor](https://github.com/mailorq) — fullstack
