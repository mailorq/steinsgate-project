#!/usr/bin/env bash
#
# единый прогон нагрузочного теста: поднимает прод-подобный стек (postgres +
# redis + gunicorn за nginx), наполняет БД, снимает профиль n+1 и гоняет Locust
# лестницей нагрузки, затем убирает тестовые данные
#
# backend/loadtest/run_loadtest.sh                 # 50 -> 200 -> 500, по 60с
# backend/loadtest/run_loadtest.sh --stages "100 500 1000" --time 90s
# backend/loadtest/run_loadtest.sh --keep          # не удалять данные и стек
# backend/loadtest/run_loadtest.sh --down          # только остановить стек
#
set -euo pipefail

# расположение и параметры

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT_DIR"

STAGES="50 200 500"
DURATION="60s"
SEED_USERS=500
COMMENTS_PER_TITLE=150
KEEP=0
DOWN_ONLY=0
OUT_DIR="$ROOT_DIR/loadtest_out"

# отдельный проект и порт: свои контейнеры/тома, не трогает локальный стек
PROJECT="steinsgate_loadtest"
export APP_PORT="${LOADTEST_APP_PORT:-4273}"
BASE_URL="http://localhost:${APP_PORT}"

COMPOSE=(docker compose -p "$PROJECT" -f compose.yaml -f compose.loadtest.yaml)

# python из venv проекта (Locust запускается на хосте, не в контейнере)
if [ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON="$ROOT_DIR/.venv/Scripts/python.exe"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
else
  PYTHON="python"
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --stages) STAGES="$2"; shift 2 ;;
    --time) DURATION="$2"; shift 2 ;;
    --users) SEED_USERS="$2"; shift 2 ;;
    --comments) COMMENTS_PER_TITLE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --down) DOWN_ONLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# предполетные проверки

command -v docker >/dev/null 2>&1 || die "docker не найден в PATH"
docker info >/dev/null 2>&1 || die "Docker daemon недоступен. Запустите Docker Desktop."
[ -f "$ROOT_DIR/.env" ] || die ".env не найден в корне. Скопируйте .env.example и заполните."
"$PYTHON" -c "import locust" 2>/dev/null || die "locust не установлен: (cd backend && python -m pip install -e '.[dev]')"

if [ "$DOWN_ONLY" = "1" ]; then
  log "Останавливаю изолированный стек и удаляю его тома"
  "${COMPOSE[@]}" down -v
  exit 0
fi

mkdir -p "$OUT_DIR"

cleanup() {
  local code=$?
  if [ "$KEEP" = "1" ]; then
    warn "Оставляю изолированный стек (--keep). Остановить: ${COMPOSE[*]} down -v"
  else
    log "Останавливаю изолированный стек и удаляю его тома"
    "${COMPOSE[@]}" down -v || warn "down не удался"
  fi
  exit $code
}
trap cleanup EXIT

# поднятие стека

log "Поднимаю стек (Postgres + Redis + gunicorn + nginx)"
"${COMPOSE[@]}" up -d --build

log "Жду готовности API на $BASE_URL"
ready=0
for _ in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE_URL/api/anime" 2>/dev/null || true)
  if [ "$code" = "200" ]; then ready=1; break; fi
  sleep 2
done
[ "$ready" = "1" ] || { "${COMPOSE[@]}" logs --tail 40 backend; die "API не поднялся за отведённое время"; }
log "API готов"

# наполнение и профиль

log "Сидинг: $SEED_USERS юзеров, $COMMENTS_PER_TITLE комментариев на тайтл"
"${COMPOSE[@]}" exec -T backend python manage.py seed_loadtest \
  --users "$SEED_USERS" --comments "$COMMENTS_PER_TITLE"

log "Профиль числа SQL-запросов (детектор N+1)"
"${COMPOSE[@]}" exec -T backend python manage.py profile_queries | tee "$OUT_DIR/query_profile.txt"

# лестница нагрузки

for users in $STAGES; do
  rate=$(( users / 10 )); [ "$rate" -lt 1 ] && rate=1
  tag="u${users}"
  log "Locust: $users одновременных пользователей, рэмп $rate/с, $DURATION"
  LOADTEST_USERS="$SEED_USERS" "$PYTHON" -m locust \
    -f "$SCRIPT_DIR/locustfile.py" \
    --host "$BASE_URL" \
    --headless -u "$users" -r "$rate" -t "$DURATION" \
    --csv "$OUT_DIR/$tag" --html "$OUT_DIR/report_$tag.html" \
    --only-summary
done

# сводка

log "Готово. Отчёты в: $OUT_DIR"
printf '\n%-8s %-10s %-10s %-10s %-10s\n' "юзеров" "RPS" "p95(ms)" "p99(ms)" "%ошибок"
for users in $STAGES; do
  f="$OUT_DIR/u${users}_stats.csv"
  [ -f "$f" ] || continue
  "$PYTHON" - "$users" "$f" <<'PY'
import csv, sys
users, path = sys.argv[1], sys.argv[2]
with open(path, newline="") as fh:
    for row in csv.DictReader(fh):
        if row.get("Name") == "Aggregated":
            reqs = float(row["Request Count"] or 0)
            fails = float(row["Failure Count"] or 0)
            pct = (fails / reqs * 100) if reqs else 0.0
            print(f'{users:<8} {float(row["Requests/s"]):<10.1f} '
                  f'{row["95%"]:<10} {row["99%"]:<10} {pct:<10.2f}')
PY
done
echo
