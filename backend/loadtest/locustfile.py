"""
нагрузочный сценарий для steins gate api.

моделирует реальную сессию зрителя: просмотр страниц тайтлов, автосохранение
прогресса, чтение и написание комментариев, оценки, реакции и регистрацию
веса подобраны под реальное поведение - read-dominant, но с активными
комментариями и оценками

запуск (см. loadtest/README.md — там же нейтрализация лимитов и мок почты):

locust -f backend/loadtest/locustfile.py --host http://localhost:4173

предохранители: против нелокального хоста не стартует без LOADTEST_ALLOW_REMOTE=1
требует заранее засеянных пользователей: python manage.py seed_loadtest
"""

import os
import random
import uuid
from urllib.parse import urlparse

from locust import HttpUser, between, events, task

SEED_USER_PREFIX = "loadtest_user_"
SEED_USER_PASSWORD = "loadtest-pass-12345"
SEED_USER_COUNT = int(os.environ.get("LOADTEST_USERS", "500"))

# регистрация: пароль обязан проходить валидаторы django
REGISTER_PASSWORD = "Loadtest-Pass-9421"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "backend", "::1"}


@events.init.add_listener
def _guard_target_host(environment, **_kwargs):
    host = environment.host or ""
    hostname = urlparse(host).hostname or ""
    if hostname in LOCAL_HOSTS:
        return
    if os.environ.get("LOADTEST_ALLOW_REMOTE") == "1":
        print(f"[loadtest] Нелокальный хост {hostname} разрешён через LOADTEST_ALLOW_REMOTE=1")
        return
    raise SystemExit(
        f"[loadtest] Отказ: хост '{hostname}' не локальный. Нагрузочный тест "
        "предназначен для локального/staging окружения. Для осознанного запуска "
        "против другого хоста выставьте LOADTEST_ALLOW_REMOTE=1."
    )


class Viewer(HttpUser):
    # пауза «подумать» между действиями - иначе это стресс, а не имитация людей
    wait_time = between(1, 5)

    def on_start(self):
        self.slugs: list[str] = []
        self.known_comment_ids: list[int] = []
        self.logged_in = False

        self._prime_csrf()
        self._load_catalog()
        self._login()

    # служебное

    def _csrf_headers(self) -> dict:
        token = self.client.cookies.get("csrftoken", "")
        return {"X-CSRFToken": token}

    def _prime_csrf(self):
        # ставит csrftoken-cookie; дальше он едет в X-CSRFToken на мутациях
        self.client.get("/api/auth/csrf", name="/api/auth/csrf")

    def _load_catalog(self):
        with self.client.get("/api/anime", name="/api/anime", catch_response=True) as resp:
            if resp.status_code == 200 and isinstance(resp.json(), list):
                self.slugs = [item["slug"] for item in resp.json() if "slug" in item]
                if self.slugs:
                    resp.success()
                else:
                    resp.failure("Каталог пуст — сначала seed_loadtest")
            else:
                resp.failure(f"Каталог недоступен: {resp.status_code}")

    def _login(self):
        username = f"{SEED_USER_PREFIX}{random.randint(0, max(SEED_USER_COUNT - 1, 0))}"
        with self.client.post(
            "/api/auth/login",
            json={"username": username, "password": SEED_USER_PASSWORD},
            headers=self._csrf_headers(),
            name="/api/auth/login",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                self.logged_in = True
                resp.success()
            else:
                # не валим прогон: аноним все равно нагружает read-ручки
                resp.failure(f"Логин не удался ({resp.status_code}) — проверьте сидинг")

    def _pick_slug(self) -> str | None:
        return random.choice(self.slugs) if self.slugs else None

    # сценарии (веса = реальное поведение)

    @task(12)
    def open_title_page(self):
        slug = self._pick_slug()
        if not slug:
            return
        self.client.get(f"/api/anime/{slug}", name="/api/anime/[slug]")
        with self.client.post(
            f"/api/anime/{slug}/view",
            headers=self._csrf_headers(),
            name="/api/anime/[slug]/view",
            catch_response=True,
        ) as resp:
            if resp.status_code == 204:
                resp.success()
            else:
                resp.failure(f"Просмотр: {resp.status_code}")
        with self.client.get(
            f"/api/anime/{slug}/comments?page=1",
            name="/api/anime/[slug]/comments",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                self._remember_comments(resp)
                resp.success()
            else:
                resp.failure(f"Комментарии: {resp.status_code}")
        if self.logged_in:
            self.client.get(f"/api/anime/{slug}/progress", name="/api/anime/[slug]/progress")

    @task(8)
    def save_progress(self):
        """автосохранение во время просмотра - частый write"""
        slug = self._pick_slug()
        if not slug or not self.logged_in:
            return
        current = random.randint(30, 1400)
        self.client.put(
            f"/api/anime/{slug}/progress",
            json={"current_time": current, "duration": 1440},
            headers=self._csrf_headers(),
            name="/api/anime/[slug]/progress",
        )

    @task(6)
    def browse_comments(self):
        """листание страниц комментариев"""
        slug = self._pick_slug()
        if not slug:
            return
        page = random.randint(1, 5)
        with self.client.get(
            f"/api/anime/{slug}/comments?page={page}",
            name="/api/anime/[slug]/comments",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                self._remember_comments(resp)
                resp.success()
            else:
                resp.failure(f"Комментарии: {resp.status_code}")

    @task(6)
    def post_comment(self):
        """написание комментария - по правке заказчика, частое действие"""
        slug = self._pick_slug()
        if not slug or not self.logged_in:
            return
        with self.client.post(
            f"/api/anime/{slug}/comments",
            json={"text": f"Отличная серия, мнение №{random.randint(1, 9999)} по мировой линии."},
            headers=self._csrf_headers(),
            name="/api/anime/[slug]/comments",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                resp.success()
            else:
                resp.failure(f"POST комментария: {resp.status_code}")

    @task(6)
    def rate_title(self):
        slug = self._pick_slug()
        if not slug or not self.logged_in:
            return
        self.client.post(
            f"/api/anime/{slug}/rating",
            json={"rating": random.randint(1, 5)},
            headers=self._csrf_headers(),
            name="/api/anime/[slug]/rating",
        )

    @task(4)
    def react_to_comment(self):
        """лайк/дизлайк комментария"""
        if not self.logged_in or not self.known_comment_ids:
            return
        comment_id = random.choice(self.known_comment_ids)
        with self.client.post(
            f"/api/comments/{comment_id}/reaction",
            json={"is_like": random.random() > 0.3},
            headers=self._csrf_headers(),
            name="/api/comments/[id]/reaction",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Реакция: {resp.status_code}")

    @task(3)
    def browse_catalog(self):
        self.client.get("/api/anime", name="/api/anime")

    @task(2)
    def register_account(self):
        marker = uuid.uuid4().hex[:12]
        username = f"loadtest_reg_{marker}"
        with self.client.post(
            "/api/auth/register",
            json={
                "username": username,
                "email": f"{username}@gmail.com",
                "password": REGISTER_PASSWORD,
            },
            headers=self._csrf_headers(),
            name="/api/auth/register",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                resp.success()
            else:
                resp.failure(f"Регистрация: {resp.status_code}")

    # helpers

    def _remember_comments(self, response):
        try:
            items = response.json().get("items", [])
        except ValueError:
            return
        ids = [item["id"] for item in items if "id" in item]
        if ids:
            # держим ограниченный пул, чтобы не копить память в долгом прогоне
            self.known_comment_ids = (self.known_comment_ids + ids)[-200:]
