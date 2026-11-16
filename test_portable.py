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
    def test_shell_and_templates(self):
        for script in DEPLOY.rglob("*.sh"):
            subprocess.run(["bash", "-n", str(script)], check=True)
        values = dict.fromkeys(("root", "service", "user", "venv", "workers", "static",
                                "media", "domain", "settings_module", "wsgi_module"), "test")
        for template in (DEPLOY / "templates").iterdir():
            self.assertIn("test", template.read_text().format(**values))

    def test_custom_modules_and_health_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "shop"
            project.mkdir()
            (project / "settings.py").touch()
            (project / "wsgi.py").touch()
            env = root / ".env"
            env.touch(mode=0o600)
            values = {
                "SECRET_KEY": "s" * 50, "DEBUG": "False", "DEPLOY_SETTINGS_MODULE": "shop.settings",
                "DEPLOY_WSGI_MODULE": "shop.wsgi", "DEPLOY_HEALTH_PATH": "/ready/",
                "APP_DOMAIN": "shop.test", "ALLOWED_HOSTS": "shop.test", "DB_NAME": "shop",
                "DB_USER": "shop", "DB_PASSWORD": "secret", "DB_HOST": "127.0.0.1",
                "DB_PORT": "5432", "REDIS_URL": "redis://127.0.0.1:6379/0",
                "STATIC_ROOT": str(root / "staticfiles"), "MEDIA_ROOT": str(root / "media"),
            }
            with (patch.object(native, "ROOT", root), patch.object(native, "ENV", env),
                  patch.object(native, "USER", pwd.getpwuid(os.getuid()).pw_name)):
                native.validate_environment(values)
                for key, bad in (("DEPLOY_SETTINGS_MODULE", "other.settings"),
                                 ("DEPLOY_WSGI_MODULE", "../bad"),
                                 ("DEPLOY_HEALTH_PATH", "//other-host/")):
                    changed = {**values, key: bad}
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        native.validate_environment(changed)


if __name__ == "__main__":
    unittest.main()
