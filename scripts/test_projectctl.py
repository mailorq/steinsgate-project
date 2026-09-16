"""
Тесты projectctl

python -m unittest discover -s scripts -p "test_*.py"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "projectctl", Path(__file__).resolve().parent / "projectctl.py"
)
ctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctl)

# сервисы с env_file: .env; интеграционный стек переопределяет им файл окружения
DJANGO_SERVICES = ("migrate", "backend", "celery-worker", "celery-beat")


def valid_env(**overrides: str) -> dict[str, str]:
    env = {
        "PROJECTCTL_MODE": "demo",
        "SECRET_KEY": ctl.generate_secret(),
        "EMAIL_DELIVERY_QUOTA_SECRET": ctl.generate_secret(),
        "DB_PASSWORD": ctl.generate_secret(),
        "APP_PORT": "4173",
        "DEBUG": "False",
        "ALLOWED_HOSTS": "localhost,127.0.0.1",
        "EMAIL_HOST_USER": "user@gmail.com",
        "EMAIL_HOST_PASSWORD": "apppassword",
    }
    env.update(overrides)
    return env


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class SecretGenerationTest(unittest.TestCase):

    def test_secret_is_long_and_url_safe(self):
        for _ in range(20):
            value = ctl.generate_secret()
            self.assertGreaterEqual(len(value), ctl.MIN_SECRET_LENGTH)
            self.assertNotIn("$", value)
            self.assertTrue(all(c.isalnum() or c in "-_" for c in value))

    def test_secrets_are_unique(self):
        self.assertEqual(len({ctl.generate_secret() for _ in range(50)}), 50)

    def test_generated_secret_passes_validation(self):
        self.assertEqual(ctl.validate_secret(ctl.generate_secret()), [])


class SecretValidationTest(unittest.TestCase):

    def test_empty_rejected(self):
        self.assertTrue(ctl.validate_secret(""))

    def test_short_rejected(self):
        self.assertTrue(ctl.validate_secret("abc123"))

    def test_dollar_sign_rejected(self):
        self.assertTrue(any("$" in p for p in ctl.validate_secret("a" * 40 + "$y" + "b" * 20)))

    def test_quoted_rejected(self):
        self.assertTrue(ctl.validate_secret('"' + "a" * 60 + '"'))

    def test_whitespace_rejected(self):
        self.assertTrue(ctl.validate_secret("a" * 30 + " " + "b" * 30))


class InterpolationSafetyTest(unittest.TestCase):

    def test_db_password_with_dollar_is_error(self):
        problems, _ = ctl.validate_env_values(
            valid_env(DB_PASSWORD="pa$$word-value-1234567890"), allow_debug=False
        )
        self.assertTrue(any("DB_PASSWORD" in p and "$" in p for p in problems))

    def test_email_password_with_dollar_is_error(self):
        problems, _ = ctl.validate_env_values(
            valid_env(EMAIL_HOST_PASSWORD="ab$cdef"), allow_debug=False
        )
        self.assertTrue(any("EMAIL_HOST_PASSWORD" in p for p in problems))

    def test_quota_secret_is_required_and_interpolation_safe(self):
        problems, _ = ctl.validate_env_values(
            valid_env(EMAIL_DELIVERY_QUOTA_SECRET=""), allow_debug=False
        )
        self.assertTrue(any("EMAIL_DELIVERY_QUOTA_SECRET" in p for p in problems))

        problems, _ = ctl.validate_env_values(
            valid_env(EMAIL_DELIVERY_QUOTA_SECRET="a" * 55 + "$"), allow_debug=False
        )
        self.assertTrue(any("EMAIL_DELIVERY_QUOTA_SECRET" in p and "$" in p for p in problems))

    def test_db_password_quoted_is_error(self):
        problems, _ = ctl.validate_env_values(
            valid_env(DB_PASSWORD='"quoted-password-value"'), allow_debug=False
        )
        self.assertTrue(any("DB_PASSWORD" in p for p in problems))

    def test_email_password_allows_internal_space(self):
        problems, _ = ctl.validate_env_values(
            valid_env(EMAIL_HOST_PASSWORD="abcd efgh ijkl"), allow_debug=False
        )
        self.assertEqual(problems, [])

    def test_email_password_edge_space_is_error(self):
        problems, _ = ctl.validate_env_values(
            valid_env(EMAIL_HOST_PASSWORD=" abcdefgh"), allow_debug=False
        )
        self.assertTrue(any("EMAIL_HOST_PASSWORD" in p for p in problems))

    def test_newline_in_value_is_error(self):
        self.assertTrue(ctl.validate_interpolation_safe("DB_PASSWORD", "a\nB=1"))

    def test_all_interpolated_keys_are_covered(self):
        for key in ctl.INTERPOLATED_SECRETS:
            self.assertTrue(ctl.validate_interpolation_safe(key, "va$lue"))


class HostValidationTest(unittest.TestCase):

    def test_plain_hostname(self):
        self.assertEqual(ctl.validate_host("steins.example.com"), ("steins.example.com", []))

    def test_ipv4(self):
        self.assertEqual(ctl.validate_host("192.0.2.10"), ("192.0.2.10", []))

    def test_ipv6_is_bracketed(self):
        host, problems = ctl.validate_host("::1")
        self.assertEqual(problems, [])
        self.assertEqual(host, "[::1]")

    def test_newline_injection_rejected(self):
        host, problems = ctl.validate_host("example.com\nDEBUG=True")
        self.assertEqual(host, "")
        self.assertTrue(problems)

    def test_carriage_return_injection_rejected(self):
        self.assertTrue(ctl.validate_host("example.com\rSECRET_KEY=x")[1])

    def test_equals_injection_rejected(self):
        self.assertTrue(ctl.validate_host("example.com=x")[1])

    def test_scheme_rejected(self):
        self.assertTrue(ctl.validate_host("https://example.com")[1])

    def test_port_rejected(self):
        self.assertTrue(ctl.validate_host("example.com:8080")[1])

    def test_path_rejected(self):
        self.assertTrue(ctl.validate_host("example.com/admin")[1])

    def test_space_rejected(self):
        self.assertTrue(ctl.validate_host("exa mple.com")[1])

    def test_dollar_rejected(self):
        self.assertTrue(ctl.validate_host("$HOST")[1])

    def test_empty_rejected(self):
        self.assertTrue(ctl.validate_host("")[1])

    def test_injected_host_never_reaches_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            args = argparse.Namespace(
                mode="production", host="example.com\nDEBUG=True", port=4173,
                rotate_secret=False, allow_debug=False,
            )
            with mock.patch.object(ctl, "env_path", return_value=path):
                with self.assertRaises(ctl.CtlError):
                    ctl.cmd_init(args)
            self.assertFalse(path.exists())


class PortValidationTest(unittest.TestCase):

    def test_valid_port(self):
        self.assertEqual(ctl.validate_port("4173"), (4173, []))

    def test_non_numeric(self):
        self.assertTrue(ctl.validate_port("abc")[1])

    def test_out_of_range(self):
        self.assertTrue(ctl.validate_port("70000")[1])
        self.assertTrue(ctl.validate_port("0")[1])

    def test_missing(self):
        self.assertTrue(ctl.validate_port("")[1])


class PortConflictTest(unittest.TestCase):

    def _serve(self, family, address):
        server = socket.socket(family)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(address)
        server.listen(1)
        port = server.getsockname()[1]
        stop = threading.Event()

        def loop():
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                    connection.close()
                except OSError:
                    pass

        worker = threading.Thread(target=loop, daemon=True)
        worker.start()
        self.addCleanup(server.close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(stop.set)
        return port

    def test_free_port_reported_free(self):
        self.assertTrue(ctl.port_is_free(free_port()))

    def test_ipv4_loopback_conflict(self):
        port = self._serve(socket.AF_INET, ("127.0.0.1", 0))
        self.assertFalse(ctl.port_is_free(port))

    def test_ipv4_wildcard_conflict(self):
        port = self._serve(socket.AF_INET, ("0.0.0.0", 0))
        self.assertFalse(ctl.port_is_free(port))

class EnvValidationTest(unittest.TestCase):

    def test_valid_env_has_no_problems(self):
        problems, _ = ctl.validate_env_values(valid_env(), allow_debug=False)
        self.assertEqual(problems, [])

    def test_debug_blocked_without_acknowledgement(self):
        problems, _ = ctl.validate_env_values(valid_env(DEBUG="True"), allow_debug=False)
        self.assertTrue(any("DEBUG" in p for p in problems))

    def test_debug_allowed_in_demo_with_acknowledgement(self):
        problems, warnings = ctl.validate_env_values(valid_env(DEBUG="True"), allow_debug=True)
        self.assertEqual(problems, [])
        self.assertTrue(any("127.0.0.1" in w for w in warnings))

    def test_debug_forbidden_in_production_even_with_flag(self):
        env = valid_env(DEBUG="True", PROJECTCTL_MODE="production")
        problems, _ = ctl.validate_env_values(env, allow_debug=True)
        self.assertTrue(any("production" in p for p in problems))

    def test_https_is_required_in_production(self):
        env = valid_env(HTTPS_ENABLED="False", PROJECTCTL_MODE="production")
        problems, _ = ctl.validate_env_values(env, allow_debug=False)
        self.assertTrue(any("HTTPS_ENABLED=True" in p for p in problems))

    def test_debug_without_mode_marker_falls_back_to_demo(self):
        env = valid_env(DEBUG="True")
        env.pop("PROJECTCTL_MODE")
        self.assertEqual(ctl.resolve_mode(env), "demo")

    def test_unknown_mode_is_error(self):
        for value in ("demoo", "prod", "PRODUCTION ", "x"):
            with self.subTest(value=value):
                env = valid_env(PROJECTCTL_MODE=value)
                if value.strip().lower() in ("demo", "production"):
                    continue
                with self.assertRaises(ctl.CtlError):
                    ctl.resolve_mode(env)

    def test_known_mode_is_case_insensitive(self):
        self.assertEqual(ctl.resolve_mode(valid_env(PROJECTCTL_MODE="Demo")), "demo")
        self.assertEqual(ctl.resolve_mode(valid_env(PROJECTCTL_MODE="PRODUCTION")), "production")

    def test_no_project_name_env_override(self):
        source = (Path(__file__).resolve().parent / "projectctl.py").read_text(encoding="utf-8")
        self.assertNotIn("PROJECTCTL_PROJECT", source)
        self.assertIn('PROJECT_NAME = "steinsgate_mailor"', source)

    def test_missing_db_password(self):
        problems, _ = ctl.validate_env_values(valid_env(DB_PASSWORD=""), allow_debug=False)
        self.assertTrue(any("DB_PASSWORD" in p for p in problems))

    def test_bad_allowed_hosts_rejected(self):
        problems, _ = ctl.validate_env_values(
            valid_env(ALLOWED_HOSTS="https://evil.example.com"), allow_debug=False
        )
        self.assertTrue(any("ALLOWED_HOSTS" in p for p in problems))

    def test_wildcard_allowed_hosts_warns_in_demo(self):
        problems, warnings = ctl.validate_env_values(
            valid_env(ALLOWED_HOSTS="*"), allow_debug=False
        )
        self.assertEqual(problems, [])
        self.assertTrue(any("Host" in w for w in warnings))

    def test_wildcard_allowed_hosts_forbidden_in_production(self):
        env = valid_env(ALLOWED_HOSTS="*", PROJECTCTL_MODE="production")
        problems, _ = ctl.validate_env_values(env, allow_debug=False)
        self.assertTrue(any("ALLOWED_HOSTS=*" in p for p in problems))

    def test_empty_email_is_warning_not_error(self):
        problems, warnings = ctl.validate_env_values(
            valid_env(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD=""), allow_debug=False
        )
        self.assertEqual(problems, [])
        self.assertTrue(any("EMAIL" in w for w in warnings))

    def test_no_secret_values_leak_into_messages(self):
        secret = ctl.generate_secret()
        env = valid_env(SECRET_KEY=secret, DEBUG="True")
        problems, warnings = ctl.validate_env_values(env, allow_debug=False)
        for message in problems + warnings:
            self.assertNotIn(secret, message)
            self.assertNotIn(env["DB_PASSWORD"], message)


class EnvFileTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_env_is_parseable_and_valid(self):
        ctl.write_env_atomic(
            self.path, ctl.render_env(mode="demo", host="", port=4173, debug=False, https=False)
        )
        env = ctl.read_env(self.path)
        problems, _ = ctl.validate_env_values(env, allow_debug=False)
        self.assertEqual(problems, [])
        self.assertEqual(env["PROJECTCTL_MODE"], "demo")

    def test_production_mode_uses_host_and_https(self):
        ctl.write_env_atomic(
            self.path,
            ctl.render_env(mode="production", host="steins.example.com", port=8080,
                           debug=False, https=True),
        )
        env = ctl.read_env(self.path)
        self.assertEqual(env["ALLOWED_HOSTS"], "steins.example.com")
        self.assertEqual(env["CSRF_TRUSTED_ORIGINS"], "https://steins.example.com")
        self.assertEqual(env["NINJA_NUM_PROXIES"], "2")

    def test_init_production_always_enables_https(self):
        args = argparse.Namespace(
            mode="production",
            host="steins.example.com",
            port=4173,
            rotate_secret=False,
            allow_debug=False,
        )

        with mock.patch.object(ctl, "env_path", return_value=self.path):
            self.assertEqual(ctl.cmd_init(args), 0)

        self.assertEqual(ctl.read_env(self.path)["HTTPS_ENABLED"], "True")

    def test_render_env_generates_distinct_secrets(self):
        ctl.write_env_atomic(
            self.path, ctl.render_env(mode="demo", host="", port=4173, debug=False, https=False)
        )
        env = ctl.read_env(self.path)
        self.assertNotEqual(env["SECRET_KEY"], env["DB_PASSWORD"])
        self.assertNotEqual(env["SECRET_KEY"], env["EMAIL_DELIVERY_QUOTA_SECRET"])

    def test_set_env_value_replaces_and_keeps_comments(self):
        ctl.write_env_atomic(self.path, "# комментарий\nSECRET_KEY=old\nAPP_PORT=4173\n")
        ctl.set_env_value(self.path, "SECRET_KEY", "new")
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("# комментарий", content)
        self.assertEqual(ctl.read_env(self.path)["SECRET_KEY"], "new")
        self.assertEqual(ctl.read_env(self.path)["APP_PORT"], "4173")

    def test_set_env_value_rejects_newline(self):
        ctl.write_env_atomic(self.path, "A=1\n")
        with self.assertRaises(ctl.CtlError):
            ctl.set_env_value(self.path, "A", "x\nB=2")

    def test_init_preserves_legacy_quota_budget_before_secret_rotation(self):
        old_secret = ctl.generate_secret()
        ctl.write_env_atomic(self.path, f"SECRET_KEY={old_secret}\n")
        args = argparse.Namespace(
            mode="demo",
            host=None,
            port=4173,
            rotate_secret=True,
            allow_debug=False,
        )

        with mock.patch.object(ctl, "env_path", return_value=self.path):
            self.assertEqual(ctl.cmd_init(args), 0)

        env = ctl.read_env(self.path)
        self.assertEqual(env["EMAIL_DELIVERY_QUOTA_SECRET"], old_secret)
        self.assertNotEqual(env["SECRET_KEY"], old_secret)

    def test_init_refuses_a_symlink_before_reading_it(self):
        args = argparse.Namespace(
            mode="demo",
            host=None,
            port=4173,
            rotate_secret=False,
            allow_debug=False,
        )

        with mock.patch.object(ctl, "env_path", return_value=self.path), \
             mock.patch.object(Path, "is_symlink", return_value=True), \
             mock.patch.object(ctl, "read_env") as read_env:
            with self.assertRaises(ctl.CtlError):
                ctl.cmd_init(args)

        read_env.assert_not_called()

    def test_read_env_ignores_comments_and_blanks(self):
        ctl.write_env_atomic(self.path, "# c\n\nA=1\n  B=2\n")
        self.assertEqual(ctl.read_env(self.path), {"A": "1", "B": "2"})


class SafeWriteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlink_target_is_refused(self):
        target = self.dir / "outside.txt"
        target.write_text("original", encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink недоступен")
        with self.assertRaises(ctl.CtlError):
            ctl.write_env_atomic(self.path, "A=1\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_symlink_refused_without_creating_one(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ctl.CtlError) as caught:
                ctl.write_env_atomic(self.path, "A=1\n")
        self.assertIn("символическ", str(caught.exception))

    def test_directory_target_is_refused(self):
        (self.dir / "envdir").mkdir()
        with self.assertRaises(ctl.CtlError):
            ctl.write_env_atomic(self.dir / "envdir", "A=1\n")

    @unittest.skipIf(os.name == "nt", "POSIX-права")
    def test_permissions_are_600(self):
        ctl.write_env_atomic(self.path, "A=1\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_temporary_files_left(self):
        ctl.write_env_atomic(self.path, "A=1\n")
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failed_write_keeps_original(self):
        ctl.write_env_atomic(self.path, "ORIGINAL=1\n")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ctl.write_env_atomic(self.path, "REPLACED=1\n")
        self.assertIn("ORIGINAL", self.path.read_text(encoding="utf-8"))


class JsonStrictnessTest(unittest.TestCase):

    def test_parse_json_strict_raises_on_garbage(self):
        with self.assertRaises(ctl.CtlError):
            ctl.parse_json_strict("not json", "docker compose config")

    def test_parse_json_strict_raises_on_empty(self):
        with self.assertRaises(ctl.CtlError):
            ctl.parse_json_strict("   ", "docker compose config")

    def test_exposure_check_raises_on_unparseable_config(self):
        with mock.patch.object(ctl, "compose", return_value=completed(0, "<<garbage>>")):
            with self.assertRaises(ctl.CtlError):
                ctl.check_service_exposure("demo")

    def test_exposure_check_raises_without_services(self):
        with mock.patch.object(ctl, "compose", return_value=completed(0, '{"version":"3"}')):
            with self.assertRaises(ctl.CtlError):
                ctl.check_service_exposure("demo")

    def test_exposure_check_raises_when_compose_fails(self):
        with mock.patch.object(ctl, "compose", return_value=completed(1, "")):
            with self.assertRaises(ctl.CtlError):
                ctl.check_service_exposure("demo")

    def test_exposure_flags_published_backend(self):
        config = json.dumps({"services": {
            "backend": {"ports": [{"host_ip": "0.0.0.0", "published": "8000"}]},
            "frontend": {"ports": [{"host_ip": "127.0.0.1", "published": "4173"}]},
        }})
        with mock.patch.object(ctl, "compose", return_value=completed(0, config)):
            problems = ctl.check_service_exposure("demo")
        self.assertTrue(any("backend" in p for p in problems))

    def test_any_mode_flags_public_frontend_binding(self):
        config = json.dumps({"services": {
            "frontend": {"ports": [{"host_ip": "0.0.0.0", "published": "4173"}]},
        }})
        with mock.patch.object(ctl, "compose", return_value=completed(0, config)):
            problems = ctl.check_service_exposure("production")
        self.assertTrue(any("127.0.0.1" in p for p in problems))

    def test_iter_json_objects_raises_on_bad_line(self):
        with self.assertRaises(ctl.CtlError):
            ctl.iter_json_objects('{"a":1}\nnot-json')


class ExitCodeTest(unittest.TestCase):

    def test_status_fails_when_compose_fails(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "check_ownership", return_value=[]), \
             mock.patch.object(ctl, "compose", return_value=completed(1)):
            self.assertEqual(ctl.main(["status"]), 1)

    def test_status_fails_when_health_unreachable(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "check_ownership", return_value=[]), \
             mock.patch.object(ctl, "compose", return_value=completed(0)), \
             mock.patch.object(ctl, "project_publishes_port", return_value=True), \
             mock.patch.object(ctl, "read_env", return_value={"APP_PORT": str(free_port())}):
            self.assertEqual(ctl.main(["status"]), 1)

    def test_status_rejects_an_http_response_from_an_unpublished_port(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "check_ownership", return_value=[]), \
             mock.patch.object(ctl, "compose", return_value=completed(0)), \
             mock.patch.object(ctl, "project_publishes_port", return_value=False), \
             mock.patch.object(ctl, "read_env", return_value={"APP_PORT": "4173"}), \
             mock.patch.object(ctl.urllib.request, "urlopen") as urlopen:
            self.assertEqual(ctl.main(["status"]), 1)

        urlopen.assert_not_called()

    def test_status_fails_when_worker_is_not_healthy(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "check_ownership", return_value=[]), \
             mock.patch.object(ctl, "compose", return_value=completed(0)), \
             mock.patch.object(ctl, "project_publishes_port", return_value=True), \
             mock.patch.object(ctl, "read_env", return_value={"APP_PORT": "4173"}), \
             mock.patch.object(ctl.urllib.request, "urlopen"), \
             mock.patch.object(ctl, "background_service_states",
                               return_value={"celery-worker": "unhealthy", "celery-beat": "running"}):
            self.assertEqual(ctl.main(["status"]), 1)

    def test_logs_fails_when_compose_fails(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "require_env"), \
             mock.patch.object(ctl, "compose", return_value=completed(1)):
            self.assertEqual(ctl.main(["logs"]), 1)

    def test_logs_succeeds_when_compose_succeeds(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "require_env"), \
             mock.patch.object(ctl, "compose", return_value=completed(0)):
            self.assertEqual(ctl.main(["logs"]), 0)


class OwnershipTest(unittest.TestCase):

    def test_foreign_workdir_detected(self):
        foreign = os.path.join(os.sep, "other", "workspace")
        with mock.patch.object(ctl, "project_containers", return_value=[("c1", foreign)]):
            problems = ctl.check_ownership()
        self.assertTrue(problems)
        self.assertIn("не доказана", problems[0])

    def test_own_workdir_accepted(self):
        with mock.patch.object(ctl, "project_containers", return_value=[("c1", str(ctl.ROOT))]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]):
            self.assertEqual(ctl.check_ownership(), [])

    def test_unlabelled_container_blocks(self):
        with mock.patch.object(ctl, "project_containers", return_value=[("c1", "")]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]):
            problems = ctl.check_ownership()
        self.assertTrue(problems)
        self.assertIn("не доказана", problems[0])

    def test_down_refuses_unlabelled_container(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[("c1", "")]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "compose") as compose_mock:
            self.assertEqual(ctl.main(["down"]), 1)
            compose_mock.assert_not_called()

    def test_adopt_refuses_unlabelled_container(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[("c1", "")]),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt", "--yes"]), 1)
            record_mock.assert_not_called()

    def test_unowned_volumes_block_operation(self):
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_postgres_data"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False):
            problems = ctl.check_ownership()
        self.assertTrue(problems)
        self.assertIn("projectctl.py adopt", problems[0])

    def test_unowned_networks_block_operation(self):
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=["x_edge"]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False):
            self.assertTrue(ctl.check_ownership())

    def test_recreated_volume_is_detected(self):
        recorded = {"resources": {"volumes": {"v": "OLD|/old"}, "networks": {}}}
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["v"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=True),              mock.patch.object(ctl, "read_state", return_value=recorded),              mock.patch.object(ctl, "_inspect", return_value="NEW|/new"):
            problems = ctl.check_ownership()
        self.assertTrue(problems)
        self.assertIn("пересоздан", problems[0])

    def test_vanished_volume_is_detected(self):
        recorded = {"resources": {"volumes": {"gone": "ID|/mnt"}, "networks": {}}}
        with mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "read_state", return_value=recorded):
            mismatches = ctl.verify_recorded_resources()
        self.assertTrue(any("исчез" in m for m in mismatches))

    def test_vanished_network_is_not_an_error(self):
        recorded = {"resources": {"volumes": {}, "networks": {"n": "ID"}}}
        with mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "read_state", return_value=recorded):
            self.assertEqual(ctl.verify_recorded_resources(), [])

    def test_check_ownership_blocks_when_all_recorded_volumes_vanish(self):
        recorded = {"resources": {"volumes": {"pg": "ID|/mnt"}, "networks": {}}}
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=True),              mock.patch.object(ctl, "read_state", return_value=recorded):
            problems = ctl.check_ownership()
        self.assertTrue(problems)
        self.assertIn("исчез", problems[0])

    def test_up_blocked_when_recorded_volume_vanished(self):
        recorded = {"resources": {"volumes": {"pg": "ID|/mnt"}, "networks": {}}}
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=True),              mock.patch.object(ctl, "read_state", return_value=recorded),              mock.patch.object(ctl, "check_service_exposure", return_value=[]),              mock.patch.object(ctl, "read_env", return_value=valid_env()),              mock.patch.object(Path, "exists", return_value=True),              mock.patch.object(Path, "is_symlink", return_value=False),              mock.patch.object(ctl, "compose") as compose_mock:
            self.assertEqual(ctl.main(["up"]), 1)
            for call in compose_mock.call_args_list:
                self.assertNotIn("up", call.args)

    def test_clean_workspace_without_marker_is_allowed(self):
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False):
            self.assertEqual(ctl.check_ownership(), [])

    def test_up_after_down_is_not_blocked(self):
        recorded = {"resources": {
            "volumes": {"pg": "ID|/mnt"},
            "networks": {"edge": "NETID"},
        }}
        with mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=True),              mock.patch.object(ctl, "read_state", return_value=recorded),              mock.patch.object(ctl, "_inspect", return_value="ID|/mnt"):
            self.assertEqual(ctl.check_ownership(), [])

    def test_unrecorded_volume_is_detected(self):
        recorded = {"resources": {"volumes": {}, "networks": {}}}
        with mock.patch.object(ctl, "project_volumes", return_value=["v"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "read_state", return_value=recorded),              mock.patch.object(ctl, "_inspect", return_value="ID"):
            self.assertTrue(ctl.verify_recorded_resources())

    def test_matching_identity_passes(self):
        recorded = {"resources": {"volumes": {"v": "ID|/mnt"}, "networks": {}}}
        with mock.patch.object(ctl, "project_volumes", return_value=["v"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "read_state", return_value=recorded),              mock.patch.object(ctl, "_inspect", return_value="ID|/mnt"):
            self.assertEqual(ctl.verify_recorded_resources(), [])

    def test_validate_never_records_ownership(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False),              mock.patch.object(ctl, "check_service_exposure", return_value=[]),              mock.patch.object(ctl, "read_env", return_value=valid_env()),              mock.patch.object(ctl, "write_state") as write_mock,              mock.patch.object(Path, "exists", return_value=True),              mock.patch.object(Path, "is_symlink", return_value=False):
            self.assertEqual(ctl.main(["validate"]), 1)
            write_mock.assert_not_called()

    def test_down_refuses_unowned_volumes(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_postgres_data"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False),              mock.patch.object(ctl, "compose") as compose_mock:
            self.assertEqual(ctl.main(["down"]), 1)
            compose_mock.assert_not_called()

    def test_up_refuses_unowned_volumes(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_postgres_data"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=False),              mock.patch.object(ctl, "check_service_exposure", return_value=[]),              mock.patch.object(ctl, "read_env", return_value=valid_env()),              mock.patch.object(Path, "exists", return_value=True),              mock.patch.object(Path, "is_symlink", return_value=False),              mock.patch.object(ctl, "compose") as compose_mock:
            self.assertEqual(ctl.main(["up"]), 1)
            for call in compose_mock.call_args_list:
                self.assertNotIn("up", call.args)

    def test_path_comparison_is_normalised(self):
        self.assertTrue(ctl._same_path(str(ctl.ROOT), str(ctl.ROOT) + os.sep))


class StateMarkerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ctl.STATE_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_records_resource_identities(self):
        with mock.patch.object(ctl, "state_path", return_value=self.path),              mock.patch.object(ctl, "project_volumes", return_value=["v"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "_inspect", return_value="ID|/mnt"):
            ctl.record_ownership()
            self.assertTrue(ctl.workspace_owns_project())
            self.assertEqual(
                ctl.read_state()["resources"]["volumes"], {"v": "ID|/mnt"}
            )

    def test_foreign_workspace_marker_rejected(self):
        self.path.write_text(
            json.dumps({"project": ctl.PROJECT_NAME, "workspace": "/elsewhere"}),
            encoding="utf-8",
        )
        with mock.patch.object(ctl, "state_path", return_value=self.path):
            self.assertFalse(ctl.workspace_owns_project())

    def test_corrupt_marker_is_not_ownership(self):
        self.path.write_text("{not json", encoding="utf-8")
        with mock.patch.object(ctl, "state_path", return_value=self.path):
            self.assertFalse(ctl.workspace_owns_project())

    def test_missing_marker_is_not_ownership(self):
        with mock.patch.object(ctl, "state_path", return_value=self.path):
            self.assertFalse(ctl.workspace_owns_project())


class AdoptCommandTest(unittest.TestCase):

    def test_adopt_requires_confirmation_word(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch("builtins.input", return_value="yes"),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt"]), 1)
            record_mock.assert_not_called()

    def test_adopt_accepts_exact_word(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch("builtins.input", return_value="ADOPT"),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt"]), 0)
            record_mock.assert_called_once()

    def test_adopt_yes_flag_skips_prompt(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch("builtins.input", side_effect=AssertionError("не должен спрашивать")),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt", "--yes"]), 0)
            record_mock.assert_called_once()

    def test_adopt_refuses_foreign_containers(self):
        foreign = os.path.join(os.sep, "other", "ws")
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[("c", foreign)]),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt", "--yes"]), 1)
            record_mock.assert_not_called()

    def test_adopt_without_resources_resets_marker(self):
        """Осознанный сброс после исчезновения тома."""
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt", "--yes"]), 0)
            record_mock.assert_called_once()

    def test_adopt_fails_without_tty(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["x_pg"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch("builtins.input", side_effect=EOFError),              mock.patch.object(ctl, "record_ownership") as record_mock:
            self.assertEqual(ctl.main(["adopt"]), 1)
            record_mock.assert_not_called()


class FailedUpRecoveryTest(unittest.TestCase):

    def test_up_skips_advisory_port_check(self):
        args = argparse.Namespace(allow_debug=False, no_build=True, timeout=30)
        with mock.patch.object(ctl, "cmd_validate") as validate_mock, \
             mock.patch.object(ctl, "read_env", return_value=valid_env()), \
             mock.patch.object(ctl, "compose", return_value=completed(0)), \
             mock.patch.object(ctl, "record_ownership"), \
             mock.patch.object(ctl, "project_publishes_port", return_value=True), \
             mock.patch.object(ctl, "wait_for_health"), \
             mock.patch.object(ctl, "wait_for_background_services"):
            self.assertEqual(ctl.cmd_up(args), 0)

        validate_mock.assert_called_once_with(args, check_port=False)

    def test_ownership_recorded_before_health_check(self):
        calls = []

        def fake_compose(*args, **kwargs):
            calls.append(args)
            return completed(0)

        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=[]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "check_service_exposure", return_value=[]),              mock.patch.object(ctl, "read_env", return_value=valid_env()),              mock.patch.object(ctl, "port_is_free", return_value=True),              mock.patch.object(ctl, "project_publishes_port", return_value=True),              mock.patch.object(ctl, "compose", side_effect=fake_compose),              mock.patch.object(ctl, "record_ownership") as record_mock,              mock.patch.object(ctl, "wait_for_health",
                               side_effect=ctl.CtlError("health не прошёл")),              mock.patch.object(Path, "exists", return_value=True),              mock.patch.object(Path, "is_symlink", return_value=False):
            self.assertEqual(ctl.main(["up"]), 1)
            record_mock.assert_called_once()

    def test_down_works_after_failed_up(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "require_env"),              mock.patch.object(ctl, "project_containers", return_value=[]),              mock.patch.object(ctl, "project_volumes", return_value=["v"]),              mock.patch.object(ctl, "project_networks", return_value=[]),              mock.patch.object(ctl, "workspace_owns_project", return_value=True),              mock.patch.object(ctl, "verify_recorded_resources", return_value=[]),              mock.patch.object(ctl, "compose", return_value=completed(0)) as compose_mock:
            self.assertEqual(ctl.main(["down"]), 0)
            compose_mock.assert_called_once()


class BackgroundServicesTest(unittest.TestCase):

    READY = {"celery-worker": "healthy", "celery-beat": "running"}

    def test_states_prefer_health_over_container_state(self):
        items = (
            {"Service": "celery-worker", "State": "running", "Health": "starting"},
            {"Service": "celery-beat", "State": "running", "Health": ""},
            {"Service": "migrate", "State": "exited", "Health": ""},
        )
        output = completed(0, "\n".join(json.dumps(item) for item in items))
        with mock.patch.object(ctl, "compose", return_value=output):
            states = ctl.background_service_states()

        self.assertEqual(
            states, {"celery-worker": "starting", "celery-beat": "running", "migrate": "exited"}
        )

    def test_unready_and_missing_services_are_reported(self):
        problems = ctl.background_service_problems({"celery-worker": "starting"})

        self.assertEqual(
            problems, {"celery-worker": "starting", "celery-beat": "контейнер не создан"}
        )

    def test_wait_returns_once_worker_becomes_healthy(self):
        states = [{**self.READY, "celery-worker": "starting"}, self.READY]
        with mock.patch.object(ctl, "background_service_states", side_effect=states), \
             mock.patch.object(ctl.time, "sleep"):
            ctl.wait_for_background_services(timeout=30)

    def test_wait_fails_fast_when_worker_crashes_on_start(self):
        with mock.patch.object(ctl, "background_service_states",
                               return_value={**self.READY, "celery-worker": "restarting"}), \
             mock.patch.object(ctl, "compose") as compose_mock, \
             mock.patch.object(ctl.time, "sleep") as sleep_mock:
            with self.assertRaises(ctl.CtlError):
                ctl.wait_for_background_services(timeout=300)

        sleep_mock.assert_not_called()
        compose_mock.assert_called_once_with("logs", "--tail", "40", "celery-worker")

    def test_wait_times_out_when_worker_never_becomes_healthy(self):
        with mock.patch.object(ctl, "background_service_states",
                               return_value={**self.READY, "celery-worker": "starting"}), \
             mock.patch.object(ctl, "compose"), \
             mock.patch.object(ctl.time, "sleep"), \
             mock.patch.object(ctl.time, "monotonic", side_effect=[0, 0, 31]):
            with self.assertRaises(ctl.CtlError):
                ctl.wait_for_background_services(timeout=30)


class PreflightTest(unittest.TestCase):

    MISSING = Path("/definitely/absent/.env")

    def test_commands_report_missing_env_clearly(self):
        for argv in (["status"], ["logs"], ["down"], ["up"]):
            with self.subTest(command=argv[0]):
                with mock.patch.object(ctl, "require_docker"),                      mock.patch.object(ctl, "env_path", return_value=self.MISSING),                      mock.patch.object(ctl, "compose") as compose_mock:
                    self.assertEqual(ctl.main(argv), 1)
                    compose_mock.assert_not_called()

    def test_missing_compose_file_is_named(self):
        with mock.patch.object(ctl, "COMPOSE_FILE", "absent.yaml"):
            with self.assertRaises(ctl.CtlError) as caught:
                ctl.require_compose_files("production")
        self.assertIn("absent.yaml", str(caught.exception))

    def test_missing_demo_override_is_named(self):
        with mock.patch.object(ctl, "DEMO_OVERRIDE", "absent-demo.yaml"):
            with self.assertRaises(ctl.CtlError) as caught:
                ctl.require_compose_files("demo")
        self.assertIn("absent-demo.yaml", str(caught.exception))

    def test_present_compose_files_pass(self):
        ctl.require_compose_files("demo")
        ctl.require_compose_files("production")

    def test_env_symlink_is_refused(self):
        with mock.patch.object(Path, "exists", return_value=True),              mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ctl.CtlError):
                ctl.require_env()

    def test_filesystem_error_is_reported_without_traceback(self):
        with mock.patch.object(ctl, "require_docker"),              mock.patch.object(ctl, "require_env"),              mock.patch.object(ctl, "compose", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(ctl.main(["status"]), 1)

    def test_failed_inspect_raises_instead_of_none(self):
        """отпечаток None молча ломал бы сверку владения"""
        with mock.patch.object(ctl, "run", return_value=completed(1, "")):
            with self.assertRaises(ctl.CtlError):
                ctl._inspect("volume", "v", "{{.Id}}")

    def test_empty_inspect_output_raises(self):
        with mock.patch.object(ctl, "run", return_value=completed(0, "   ")):
            with self.assertRaises(ctl.CtlError):
                ctl._inspect("volume", "v", "{{.Id}}")


class VolumeReuseTest(unittest.TestCase):

    def test_inspect_lists_volumes_and_never_deletes(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "project_containers", return_value=[]), \
             mock.patch.object(ctl, "project_networks", return_value=[]), \
             mock.patch.object(ctl, "project_volumes", return_value=["steinsgate_mailor_pg"]), \
             mock.patch.object(ctl, "run") as run_mock:
            self.assertEqual(ctl.main(["inspect"]), 0)
            run_mock.assert_not_called()

    def test_inspect_reports_no_entities(self):
        with mock.patch.object(ctl, "require_docker"), \
             mock.patch.object(ctl, "project_containers", return_value=[]), \
             mock.patch.object(ctl, "project_networks", return_value=[]), \
             mock.patch.object(ctl, "project_volumes", return_value=[]):
            self.assertEqual(ctl.main(["inspect"]), 0)


class ComposeFilesTest(unittest.TestCase):

    def test_demo_adds_localhost_override(self):
        files = ctl.compose_files("demo")
        self.assertTrue(any(ctl.DEMO_OVERRIDE in f for f in files))

    def test_production_has_no_override(self):
        files = ctl.compose_files("production")
        self.assertFalse(any(ctl.DEMO_OVERRIDE in f for f in files))


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class TempProjectMixin:

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = f"ctltest_{uuid.uuid4().hex[:10]}"
        cls.original_project = ctl.PROJECT_NAME
        cls.original_state = ctl.STATE_FILE
        cls.base_files = ctl.compose_files
        cls.base_env_path = ctl.env_path

        cls.workdir = Path(tempfile.mkdtemp(prefix="projectctl-it-"))
        cls.port = free_port()
        cls.env_file = cls.workdir / "test.env"
        cls.overlay = cls.workdir / "compose.override.yaml"

        ctl.write_env_atomic(
            cls.env_file,
            ctl.render_env(mode="demo", host="", port=cls.port, debug=False, https=False),
        )
        overlay = ["services:"]
        for service in DJANGO_SERVICES:
            overlay += [f"  {service}:", f"    env_file: !override [{cls.env_file.as_posix()}]"]
        cls.overlay.write_text("\n".join(overlay) + "\n", encoding="utf-8")

        ctl.PROJECT_NAME = cls.project
        ctl.STATE_FILE = f".projectctl-state-{cls.project}.json"
        ctl.env_path = lambda: cls.env_file
        ctl.compose_files = lambda mode: cls.base_files(mode) + ["-f", str(cls.overlay)]

    @classmethod
    def tearDownClass(cls):
        try:
            subprocess.run(
                [
                    "docker", "compose", "-p", cls.project,
                    "--env-file", str(cls.env_file),
                    *cls.base_files("demo"),
                    "-f", str(cls.overlay),
                    "down", "-v",
                ],
                capture_output=True, cwd=str(ctl.ROOT), timeout=300,
            )
        finally:
            (ctl.ROOT / ctl.STATE_FILE).unlink(missing_ok=True)
            shutil.rmtree(cls.workdir, ignore_errors=True)
            ctl.compose_files = cls.base_files
            ctl.env_path = cls.base_env_path
            ctl.PROJECT_NAME = cls.original_project
            ctl.STATE_FILE = cls.original_state
            super().tearDownClass()


@unittest.skipUnless(docker_available(), "Docker недоступен")
class DockerIntegrationTest(TempProjectMixin, unittest.TestCase):

    def test_compose_config_is_valid_json_for_both_modes(self):
        for mode in ("demo", "production"):
            with self.subTest(mode=mode):
                result = ctl.compose("config", "--format", "json", mode=mode, capture=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = ctl.parse_json_strict(result.stdout, "config")
                self.assertIn("services", config)

    def test_only_frontend_publishes_ports(self):
        self.assertEqual(ctl.check_service_exposure("demo"), [])

    def test_demo_override_binds_loopback_only(self):
        result = ctl.compose("config", "--format", "json", mode="demo", capture=True)
        config = ctl.parse_json_strict(result.stdout, "config")
        for entry in config["services"]["frontend"]["ports"]:
            self.assertEqual(entry.get("host_ip"), "127.0.0.1")

    def test_production_binds_loopback_only(self):
        result = ctl.compose("config", "--format", "json", mode="production", capture=True)
        config = ctl.parse_json_strict(result.stdout, "config")
        for entry in config["services"]["frontend"]["ports"]:
            self.assertEqual(entry.get("host_ip"), "127.0.0.1")

    def test_fresh_project_has_no_entities(self):
        self.assertEqual(ctl.project_containers(), [])
        self.assertEqual(ctl.project_volumes(), [])
        self.assertEqual(ctl.check_ownership(), [])

    def test_status_returns_nonzero_when_nothing_runs(self):
        with mock.patch.object(ctl, "read_env", return_value={"APP_PORT": str(free_port())}):
            self.assertEqual(ctl.main(["status"]), 1)

    def test_down_on_empty_project_is_safe(self):
        result = ctl.compose("down", mode="demo")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(ctl.project_volumes(), [])


@unittest.skipUnless(
    docker_available() and os.environ.get("PROJECTCTL_INTEGRATION_UP") == "1",
    "Тяжёлый прогон: включается PROJECTCTL_INTEGRATION_UP=1",
)
class DockerUpDownIntegrationTest(TempProjectMixin, unittest.TestCase):
    """
    блокер 5: обычный up -> health -> down без присвоения

    временное имя проекта, свободный порт и отдельный env-файл во временном
    каталоге: корневой .env проекта не читается ни инструментом, ни compose
    """

    def test_up_health_down_cycle(self):
        previous = os.environ.get("APP_PORT")
        os.environ["APP_PORT"] = str(self.port)
        try:
            self.assertEqual(ctl.main(["up", "--timeout", "300"]), 0)

            states = ctl.background_service_states()
            self.assertEqual(ctl.background_service_problems(states), {})
            self.assertEqual(states.get("migrate"), "exited")
            self.assertTrue(ctl.project_publishes_port(self.port))
            self.assertTrue(ctl.workspace_owns_project())
            recorded = ctl.read_state()["resources"]["volumes"]
            self.assertTrue(recorded)

            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{self.port}{ctl.HEALTH_PATH}", timeout=10
            ) as response:
                self.assertEqual(response.status, 200)

            self.assertEqual(ctl.main(["status"]), 0)
            self.assertEqual(ctl.main(["logs", "--tail", "5"]), 0)

            volumes_before = set(ctl.project_volumes())
            self.assertEqual(ctl.main(["down"]), 0)

            self.assertEqual(ctl.project_containers(), [])
            self.assertTrue(volumes_before.issubset(set(ctl.project_volumes())))
            self.assertEqual(ctl.main(["status"]), 1)

            self.assertEqual(ctl.main(["up", "--no-build", "--timeout", "300"]), 0)
            self.assertEqual(ctl.main(["down"]), 0)
        finally:
            if previous is None:
                os.environ.pop("APP_PORT", None)
            else:
                os.environ["APP_PORT"] = previous


if __name__ == "__main__":
    unittest.main()
