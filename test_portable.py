"""Offline checks for a copied deployment kit; no service or database changes."""
import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


DEPLOY = Path(__file__).resolve().parent
dotenv = types.ModuleType("dotenv")
dotenv.dotenv_values = lambda **kwargs: {}
parser = types.ModuleType("dotenv.parser")
parser.parse_stream = lambda stream: ()
with patch.dict(sys.modules, {"dotenv": dotenv, "dotenv.parser": parser}):
    spec = importlib.util.spec_from_file_location("portable_native", DEPLOY / "lib/native.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)


class PortableTests(unittest.TestCase):
    def test_redis_dependency_is_optional(self):
        common = str(DEPLOY / "lib/common.sh")
        prelude = ('source "$1"; environment_ready(){ :; }; postgres_ready(){ :; }; '
                   'redis_ready(){ return 7; }; ')
        absent = subprocess.run(["bash", "-c", prelude + 'native(){ return 1; }; dependencies_ready', "bash", common])
        present = subprocess.run(["bash", "-c", prelude + 'native(){ :; }; dependencies_ready', "bash", common])
        self.assertEqual(absent.returncode, 0)
        self.assertEqual(present.returncode, 7)

    def test_shell_and_templates(self):
        for script in DEPLOY.rglob("*.sh"):
            subprocess.run(["bash", "-n", str(script)], check=True)
        values = dict.fromkeys(("root", "service", "user", "venv", "workers", "static",
                                "media", "domain", "settings_module", "wsgi_module", "celery_app",
                                "redis_dependency"), "test")
        for template in (DEPLOY / "templates").iterdir():
            self.assertIn("test", template.read_text().format(**values))

    def test_custom_modules_and_health_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "shop"
            project.mkdir()
            (project / "settings.py").touch()
            (project / "wsgi.py").touch()
            (project / "celery.py").touch()
            env = root / ".env"
            env.touch(mode=0o600)
            values = {
                "SECRET_KEY": "s" * 50, "DEBUG": "False", "DEPLOY_SETTINGS_MODULE": "shop.settings",
                "DEPLOY_WSGI_MODULE": "shop.wsgi", "DEPLOY_HEALTH_PATH": "/ready/",
                "APP_DOMAIN": "shop.test", "ALLOWED_HOSTS": "shop.test", "DB_NAME": "shop",
                "DB_USER": "shop", "DB_PASSWORD": "secret", "DB_HOST": "127.0.0.1",
                "DB_PORT": "5432",
                "STATIC_ROOT": str(root / "staticfiles"), "MEDIA_ROOT": str(root / "media"),
            }
            with (patch.object(native, "ROOT", root), patch.object(native, "ENV", env),
                  patch.object(native, "USER", pwd.getpwuid(os.getuid()).pw_name)):
                native.validate_environment(values)
                self.assertEqual(native.local_redis_url(values), "")
                for key, bad in (("DEPLOY_SETTINGS_MODULE", "other.settings"),
                                 ("DEPLOY_WSGI_MODULE", "../bad"),
                                 ("DEPLOY_HEALTH_PATH", "//other-host/")):
                    changed = {**values, key: bad}
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        native.validate_environment(changed)
                with self.assertRaises(ValueError):
                    native.validate_environment({**values, "CELERY_BROKER_URL": "amqp://127.0.0.1:5672//"})
                with patch.object(native.importlib.util, "find_spec", return_value=object()):
                    celery = {**values, "CELERY_BROKER_URL": "amqp://127.0.0.1:5672//",
                              "DEPLOY_CELERY_APP": "shop.celery"}
                    native.validate_environment(celery)
                    self.assertEqual(native.local_redis_url(celery), "")
                    local = {**celery, "CELERY_BROKER_URL": "redis://127.0.0.1:6379/1"}
                    native.validate_environment(local)
                    self.assertEqual(native.local_redis_url(local), local["CELERY_BROKER_URL"])
                with patch.object(native, "read_env", return_value=values), patch.object(native, "SERVICE", "shop"), patch.object(native, "USER", "shop"):
                    files = dict(native.rendered_files())
                self.assertNotIn(Path("/etc/systemd/system/shop-celery.service"), files)
                self.assertNotIn("redis-server.service", files[Path("/etc/systemd/system/shop.service")])
                with patch.object(native, "read_env", return_value=local), patch.object(native, "SERVICE", "shop"), patch.object(native, "USER", "shop"):
                    files = dict(native.rendered_files())
                self.assertIn(Path("/etc/systemd/system/shop-celery.service"), files)
                self.assertIn("redis-server.service", files[Path("/etc/systemd/system/shop.service")])

    def test_removing_celery_config_stops_its_managed_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "etc/systemd/system/shop-celery.service"
            worker.parent.mkdir(parents=True)
            (root / "etc/nginx/sites-enabled").mkdir(parents=True)
            state = root / "state"
            state.mkdir()
            managed = state / "shop-celery.service.managed"
            text = f"# Managed by Django native deployment: {root}\n[Unit]\n"
            worker.write_text(text)
            managed.write_text(text)

            def mapped(path):
                value = str(path)
                return root / value.lstrip("/") if value.startswith("/etc/") else Path(path)

            with (patch.object(native, "Path", mapped), patch.object(native, "ROOT", root),
                  patch.object(native, "STATE", state), patch.object(native, "SERVICE", "shop"),
                  patch.object(native, "read_env", return_value={"APP_DOMAIN": "shop.test"}),
                  patch.object(native, "rendered_files", return_value=[]),
                  patch.object(native.subprocess, "run") as run):
                run.return_value.stdout = ""
                native.render()
            run.assert_any_call(["systemctl", "disable", "--now", "shop-celery.service"], check=True)
            self.assertFalse(worker.exists())
            self.assertFalse(managed.exists())


if __name__ == "__main__":
    unittest.main()
