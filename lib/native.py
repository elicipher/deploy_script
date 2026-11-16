"""Native deployment operations; invoked only by deploy shell helpers."""
import argparse
from datetime import datetime, timezone
import getpass
import io
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
SERVICE = os.environ.get("SERVICE", "")
STATE = Path(os.environ.get("STATE", "/nonexistent"))
USER = os.environ.get("APP_USER", "")
SAFE_PATH = re.compile(r"/[A-Za-z0-9/_-]+\Z")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_env():
    require(not ENV.is_symlink(), ".env must not be a symlink.")
    require(ENV.is_file(), f"Missing {ENV}; create it with the required production values before deploying.")
    text = ENV.read_text(encoding="utf-8")
    seen = set()
    for binding in parse_stream(io.StringIO(text)):
        require(not binding.error, "Invalid dotenv syntax; repair .env without displaying secrets.")
        if binding.key:
            require(binding.key not in seen, "Duplicate dotenv assignments; remove duplicates first.")
            seen.add(binding.key)
    # Disable interpolation so passwords containing ${...} remain literal.
    return dict(dotenv_values(stream=io.StringIO(text), interpolate=False))


def placeholder(value):
    return not value or value.startswith("replace-with-") or "example.com" in value


def ask(label):
    # Prompts must bypass the line-buffered log pipe or they stay invisible.
    with os.fdopen(os.open("/dev/tty", os.O_RDWR), "rb", buffering=0) as terminal:
        os.write(terminal.fileno(), label.encode("utf-8"))
        answer = terminal.readline()
        require(bool(answer), "Interactive input ended.")
        return answer.decode("utf-8").rstrip("\r\n")


def valid_host(value):
    if not value or len(value) > 253:
        return False
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                   for part in value.split("."))


def paths(values):
    for key in ("STATIC_ROOT", "MEDIA_ROOT"):
        require(not placeholder(values.get(key)), f"Set a real {key} in .env.")
    static = Path(values["STATIC_ROOT"])
    media = Path(values["MEDIA_ROOT"])
    for path in (static, media):
        require(SAFE_PATH.fullmatch(str(path)), "Static/media paths must be absolute and contain only letters, digits, /, _ and -.")
        require(path == path.resolve(), "Static/media paths must not contain symlinks or parent traversal.")
        require(path != ROOT and (ROOT in path.parents or Path("/var/www") in path.parents or Path("/srv") in path.parents),
                "Static/media must be below the checkout, /srv, or /var/www.")
        if ROOT in path.parents:
            require(path.relative_to(ROOT).parts[0] in {"media", "staticfiles"}, "Only media/ and staticfiles/ may be writable inside the checkout.")
    require(static != media and static not in media.parents and media not in static.parents,
            "Static and media paths must be separate, non-overlapping directories.")
    return static, media


def validate_environment(values):
    for key in ("SECRET_KEY", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
                "APP_DOMAIN", "ALLOWED_HOSTS", "REDIS_URL", "STATIC_ROOT", "MEDIA_ROOT"):
        require(not placeholder(values.get(key)), f"Set a real {key} in .env.")
    require(len(values["SECRET_KEY"]) >= 50, "SECRET_KEY must contain at least 50 characters; existing keys are never regenerated.")
    for key in ("DB_NAME", "DB_USER"):
        value = values[key]
        require(len(value.encode()) <= 63 and not any(ord(c) < 32 for c in value), f"Invalid {key} (PostgreSQL limit is 63 bytes).")
    require(values["DB_HOST"] in {"localhost", "127.0.0.1"}, "Native provisioning requires a local DB_HOST (localhost or 127.0.0.1). Existing values are preserved; edit .env explicitly.")
    require(values["DB_PORT"].isdigit() and 1 <= int(values["DB_PORT"]) <= 65535, "Invalid DB_PORT.")
    require(values.get("DEBUG", "").lower() == "false", "Set DEBUG=False explicitly.")
    for key in ("DEPLOY_SETTINGS_MODULE", "DEPLOY_WSGI_MODULE"):
        module = values.get(key, "")
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", module), f"Invalid {key}.")
        source = ROOT / (module.replace(".", "/") + ".py")
        require(source.is_file() or (ROOT / module.replace(".", "/") / "__init__.py").is_file(), f"Missing {key} module.")
    health_path = values.get("DEPLOY_HEALTH_PATH", "/health/")
    require(health_path.startswith("/") and not health_path.startswith("//") and not any(c in health_path for c in "?#\r\n"), "Invalid DEPLOY_HEALTH_PATH.")
    domain = values["APP_DOMAIN"]
    require(valid_host(domain), "APP_DOMAIN must be one ASCII domain or IPv4 address, without scheme or port.")
    hosts = [h.strip() for h in values["ALLOWED_HOSTS"].split(",")]
    require(domain in hosts and all(valid_host(h) for h in hosts), "ALLOWED_HOSTS must contain APP_DOMAIN and explicit hostnames/IPs only.")
    require(not values.get("REDIS_SOCKET"), "Native deployment uses localhost Redis TCP; remove REDIS_SOCKET explicitly.")
    parsed = urlsplit(values.get("REDIS_URL", ""))
    require(parsed.scheme == "redis" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port in {None, 6379},
            "REDIS_URL must address the native localhost Redis on port 6379; existing values are preserved.")
    paths(values)
    require(ENV.stat().st_mode & 0o777 == 0o600, ".env must have mode 0600.")
    require(ENV.stat().st_uid == pwd.getpwnam(USER).pw_uid, ".env must belong to the dedicated application user.")


def environment():
    read_env()
    os.chmod(ENV, 0o600)
    os.chown(ENV, pwd.getpwnam(USER).pw_uid, -1)
    validate_environment(read_env())
    print("Production .env validated; its contents were not changed.")


def load_django():
    values = read_env()
    os.environ.update({key: value for key, value in values.items() if value is not None})
    os.environ["DJANGO_SETTINGS_MODULE"] = values["DEPLOY_SETTINGS_MODULE"]
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from django.conf import settings
    require(settings.SECRET_KEY == values["SECRET_KEY"] and settings.DEBUG is False and
            values["APP_DOMAIN"] in settings.ALLOWED_HOSTS, "Django secret, DEBUG or ALLOWED_HOSTS differs from .env.")
    require(Path(settings.STATIC_ROOT) == Path(values["STATIC_ROOT"]) and
            Path(settings.MEDIA_ROOT) == Path(values["MEDIA_ROOT"]), "Django static/media paths differ from .env.")
    database = settings.DATABASES["default"]
    require(database["ENGINE"] == "django.db.backends.postgresql" and
            str(database["NAME"]) == values["DB_NAME"] and
            str(database["USER"]) == values["DB_USER"] and
            str(database["HOST"]) == values["DB_HOST"] and
            str(database["PORT"]) == values["DB_PORT"], "Django default database differs from .env provisioning target.")
    return values


def db_connect(values, name=None):
    import psycopg
    return psycopg.connect(dbname=name or values["DB_NAME"], user=values["DB_USER"],
                           password=values["DB_PASSWORD"], host=values["DB_HOST"],
                           port=values["DB_PORT"], connect_timeout=5)


# Administrator code is constant; no credentials or SQL values are put in argv.
POSTGRES_CODE = r'''
import json, sys
import psycopg
from psycopg import sql
v = json.load(sys.stdin)
try:
    with psycopg.connect(dbname="postgres", port=v["DB_PORT"], autocommit=True) as admin:
        role = admin.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=%s", (v["DB_USER"],)).fetchone()
        owner = admin.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (v["DB_NAME"],)).fetchone()
        if role:
            if role != (True, False, False, False, False, False):
                raise ValueError("Existing application role has incompatible privileges; review it manually.")
            try:
                with psycopg.connect(dbname=v["DB_NAME"] if owner else "postgres", user=v["DB_USER"], password=v["DB_PASSWORD"], host=v["DB_HOST"], port=v["DB_PORT"], connect_timeout=5):
                    pass
            except psycopg.Error:
                raise ValueError("Existing role cannot authenticate with .env credentials. No password was changed.") from None
        if owner and owner[0] != v["DB_USER"]:
            raise ValueError("Database belongs to another role; ownership is never changed automatically.")
        if not role:
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(v["DB_USER"]), sql.Literal(v["DB_PASSWORD"])))
            print("Created application role (password not logged).")
        if not owner:
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(v["DB_NAME"]), sql.Identifier(v["DB_USER"])))
            print("Created application database.")
    with psycopg.connect(dbname=v["DB_NAME"], port=v["DB_PORT"], autocommit=True) as admin:
        schema_owner = admin.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='public'").fetchone()
        if not schema_owner or schema_owner[0] not in (v["DB_USER"], "pg_database_owner", "postgres"):
            raise ValueError("public schema has incompatible ownership; review manually.")
        # Grant only on this application's schema; never revoke other tenants' grants.
        admin.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(v["DB_USER"])))
        other = admin.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m') AND pg_get_userbyid(c.relowner) <> %s LIMIT 1", (v["DB_USER"],)).fetchone()
        if other:
            raise ValueError("Existing schema objects belong to another role; no object ownership changed.")
    print("PostgreSQL role, database ownership and schema verified.")
except (ValueError, psycopg.Error) as exc:
    print(str(exc) if isinstance(exc, ValueError) else "PostgreSQL administrator operation failed; inspect server logs. No credentials reset.", file=sys.stderr)
    sys.exit(1)
'''


def postgres():
    values = read_env()
    validate_environment(values)
    if not subprocess.check_output(["pg_lsclusters", "--no-header"], text=True).strip():
        subprocess.run(["pg_createcluster", "--port", values["DB_PORT"], "--start", "16", "main"], check=True)
    credentials = {key: values[key] for key in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT")}
    result = subprocess.run(["runuser", "-u", "postgres", "--", "/usr/bin/python3", "-c", POSTGRES_CODE],
                            input=json.dumps(credentials), text=True, cwd="/")
    require(result.returncode == 0, "PostgreSQL provisioning failed; no existing passwords were reset.")
    database_check()


def database_check():
    with db_connect(read_env()) as connection:
        role = connection.execute("SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        require(role == (True, False, False, False, False, False), "Application role has incompatible privileges.")
        result = connection.execute("SELECT has_schema_privilege(current_user, 'public', 'USAGE'), has_schema_privilege(current_user, 'public', 'CREATE'), pg_get_userbyid(datdba)=current_user FROM pg_database WHERE datname=current_database()").fetchone()
        require(all(result), "Application role must own the database and have USAGE and CREATE on public schema.")
        foreign_object = connection.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m') AND pg_get_userbyid(c.relowner) <> current_user LIMIT 1").fetchone()
        require(not foreign_object, "Existing schema objects belong to another role; review ownership manually.")
    print("Application database authentication succeeded.")


def redis_check():
    import redis
    client = redis.Redis.from_url(read_env()["REDIS_URL"], socket_connect_timeout=3, socket_timeout=3)
    require(client.ping(), "Redis PING failed.")
    binds = client.config_get("bind")["bind"].split()
    require(bool(binds) and all(v.lstrip("-") in {"127.0.0.1", "::1"} for v in binds), "Redis must bind only to loopback addresses.")
    require(client.config_get("protected-mode")["protected-mode"] == "yes", "Redis protected-mode must be enabled.")
    print("Redis authentication and localhost-only configuration verified.")


def write_atomic(path, text, mode=0o644):
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    fd, name = tempfile.mkstemp(prefix=".django-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def redis_config():
    config = Path("/etc/redis/redis.conf")
    text = config.read_text()
    addition = "\n# Django native deployment: localhost-only access\nbind 127.0.0.1 -::1\nprotected-mode yes\n"
    if not text.endswith(addition):
        backup = config.with_name("redis.conf.before-django-deploy")
        if not backup.exists():
            write_atomic(backup, text, 0o600)
        # Keep the package file owner/group; Redis reads it as the redis user.
        stat = config.stat()
        write_atomic(config, text + addition, stat.st_mode & 0o777)
        os.chown(config, stat.st_uid, stat.st_gid)
        print("Restricted Redis to loopback; existing authentication/data settings preserved.")


def permissions():
    static, media = paths(read_env())
    for path in (static, media):
        path.mkdir(parents=True, exist_ok=True)
        for current, dirs, files in os.walk(path, followlinks=False):
            require(not any((Path(current) / name).is_symlink() for name in dirs + files), "Static/media contain symlinks; review permissions manually.")
        # ACLs preserve existing file ownership and grant only necessary access.
        subprocess.run(["setfacl", "-R", "-m", f"u:{USER}:rwX,u:www-data:r-X", str(path)], check=True)
        for current, dirs, files in os.walk(path, followlinks=False):
            subprocess.run(["setfacl", "-m", f"d:u:{USER}:rwx,d:u:www-data:r-x", current], check=True)
        parent = path.parent
        while parent != Path("/"):
            access = "r-x" if parent == ROOT else "--x"
            subprocess.run(["setfacl", "-m", f"u:{USER}:{access},u:www-data:--x", str(parent)], check=True)
            parent = parent.parent


def rendered_files():
    values = read_env()
    static, media = paths(values)
    # Limit memory use on small VPS hosts. Each worker needs roughly 150 MB.
    memory_kib = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1))
    workers = max(1, min(4, (os.cpu_count() or 1) * 2 + 1, memory_kib // (512 * 1024)))
    replacements = dict(root=str(ROOT), service=SERVICE, user=USER, venv=str(ROOT / ".venv-native"),
                        static=str(static), media=str(media), domain=values["APP_DOMAIN"], workers=workers,
                        settings_module=values["DEPLOY_SETTINGS_MODULE"], wsgi_module=values["DEPLOY_WSGI_MODULE"])
    for template, destination in (
        ("gunicorn.service.template", f"/etc/systemd/system/{SERVICE}.service"),
        ("nginx.conf.template", f"/etc/nginx/sites-available/{SERVICE}"),
    ):
        yield Path(destination), (DEPLOY / "templates" / template).read_text().format(**replacements)


def render(check=False):
    enabled = Path(f"/etc/nginx/sites-enabled/{SERVICE}")
    target = Path(f"/etc/nginx/sites-available/{SERVICE}")
    if check:
        for path, text in rendered_files():
            actual = path.read_text() if path.is_file() else ""
            reference = STATE / (path.name + ".managed")
            # Certbot edits the managed Nginx file. Preserve those edits while
            # its deployment inputs remain unchanged; nginx -t and health gate it.
            allowed_nginx_edit = path == target and reference.is_file() and reference.read_text() == text
            require(actual.startswith(f"# Managed by Django native deployment: {ROOT}\n") and
                    (actual == text or allowed_nginx_edit), "Managed service inputs changed; review and rerun setup/update.")
        require(enabled.is_symlink() and enabled.resolve() == target, "Managed Nginx site is not enabled.")
        return
    marker = f"# Managed by Django native deployment: {ROOT}\n"
    # nginx -T expands all includes, including virtual hosts outside sites-enabled.
    output = subprocess.run(["nginx", "-T"], capture_output=True, text=True, check=True).stdout
    domain = read_env()["APP_DOMAIN"]
    current_file = ""
    for line in output.splitlines():
        if line.startswith("# configuration file "):
            current_file = line.removeprefix("# configuration file ").removesuffix(":")
        match = re.search(r"\bserver_name\s+([^;]+);", line)
        if match and domain in match.group(1).split():
            require(Path(current_file).resolve() == target, "Another enabled Nginx site already uses APP_DOMAIN; resolve the conflict manually.")
    files = list(rendered_files())
    for path, text in files:
        require(not path.exists() or path.read_text().startswith(marker), "Refusing to overwrite an unrelated service/site.")
        if path.exists() and path.read_text() != text:
            reference = STATE / (path.name + ".managed")
            previous = reference.read_text() if reference.is_file() else ""
            require(path.read_text() == previous or (path == target and previous == text), "Managed configuration was edited manually and deployment inputs changed; reconcile before continuing (see README).")
    require(not enabled.exists() or (enabled.is_symlink() and enabled.resolve() == target), "Nginx enabled-site path is occupied.")
    for path, text in files:
        if path == target and path.exists() and path.read_text() != text and (STATE / (path.name + ".managed")).read_text() == text:
            continue
        write_atomic(path, text)
        write_atomic(STATE / (path.name + ".managed"), text, 0o600)
    if not enabled.is_symlink():
        enabled.symlink_to(target)
    print("Rendered dedicated Gunicorn unit and Nginx site; unrelated virtual hosts preserved.")


def static_check():
    values = load_django()
    from django.apps import apps
    from django.contrib.staticfiles.finders import get_finders
    from django.conf import settings
    from django.contrib.staticfiles.storage import staticfiles_storage
    require(Path(settings.STATIC_ROOT) == paths(values)[0], "STATIC_ROOT differs from configured path.")
    for finder in get_finders():
        for name, storage in finder.list(apps.get_app_config("staticfiles").ignore_patterns):
            name = "/".join(filter(None, [getattr(storage, "prefix", ""), name]))
            require(staticfiles_storage.exists(name), f"Static output is incomplete: {name}")
    for path in paths(values):
        require(os.access(path, os.R_OK | os.W_OK | os.X_OK), "Application cannot read/write static/media directories.")
    print("Static output and application directory access verified.")


def nginx_access():
    for path in paths(read_env()):
        result = subprocess.run(["runuser", "-u", "www-data", "--", "find", str(path),
                                 "(", "!", "-readable", "-o", "-type", "d", "!", "-executable", ")",
                                 "-print", "-quit"], capture_output=True, text=True)
        require(result.returncode == 0 and not result.stdout, "Nginx cannot read/traverse static/media; review ACLs.")


def superuser():
    load_django()
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.db import transaction
    User = get_user_model()
    if User.objects.filter(is_superuser=True, is_staff=True, is_active=True).exists():
        print("An active staff superuser already exists; no duplicate created.")
        return
    # Django's createsuperuser allows bypassing password validators interactively.
    # Use the real model and manager, with mandatory validation, instead.
    fields = dict.fromkeys((User.USERNAME_FIELD, *User.REQUIRED_FIELDS))
    for field in fields:
        fields[field] = ask(f"{field}: ").strip()
        require(bool(fields[field]), f"{field} is required.")
    password = getpass.getpass("Password: ")
    require(password == getpass.getpass("Password (again): "), "Passwords differ.")
    candidate = User(**fields, is_staff=True, is_superuser=True)
    candidate.set_password(password)
    candidate.full_clean()
    validate_password(password, candidate)
    with transaction.atomic():
        User.objects.create_superuser(password=password, **fields)
    print("Superuser created with model and password validation.")


def health():
    import http.client
    values = read_env()
    domain = values["APP_DOMAIN"]
    path = values.get("DEPLOY_HEALTH_PATH", "/health/")
    for direct in (True, False):
        connection = http.client.HTTPConnection("127.0.0.1", timeout=5)
        if direct:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(5)
            connection.sock.connect(f"/run/{SERVICE}/gunicorn.sock")
        headers = {"Host": domain}
        if direct and values.get("SECURE_SSL_REDIRECT", "True").lower() == "true":
            headers["X-Forwarded-Proto"] = "https"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(65536)
        connection.close()
        # HTTP redirects are verified separately through the actual TLS endpoint.
        if response.status in (301, 302, 307, 308) and values.get("SECURE_SSL_REDIRECT", "True").lower() == "true":
            require(not direct and response.getheader("Location") == f"https://{domain}{path}", "Unexpected HTTPS redirect.")
            # Connect to this host, retaining domain for certificate verification/SNI.
            import ssl
            tls = http.client.HTTPSConnection(domain, timeout=5, context=ssl.create_default_context())
            tls.sock = ssl.create_default_context().wrap_socket(socket.create_connection(("127.0.0.1", 443), timeout=5), server_hostname=domain)
            tls.request("GET", path, headers={"Host": domain})
            response = tls.getresponse()
            body = response.read(65536)
            tls.close()
        require(response.status == 200, "Health endpoint did not return HTTP 200.")
        if values.get("DEPLOY_HEALTH_JSON", "False").lower() == "true":
            data = json.loads(body)
            require(all(data.get(key) == "ok" for key in ("status", "database", "cache")), "Application database/cache health failed.")
    print("Gunicorn and Nginx readiness passed (HTTP 200).")


def backup():
    values = read_env()
    parser = argparse.ArgumentParser(description="Create or list native PostgreSQL backups")
    parser.add_argument("--label", default="manual")
    parser.add_argument("--dir", type=Path, default=Path(values.get("BACKUP_DIR") or ROOT / "backups"))
    parser.add_argument("--keep", type=int, default=int(values.get("BACKUP_KEEP") or 14))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(sys.argv[2:])
    require(os.geteuid() == 0, "Run backup with sudo.")
    require(re.fullmatch(r"[A-Za-z0-9._-]+", args.label) and args.label not in {".", ".."}, "Invalid backup label.")
    require(args.keep >= 1, "--keep must be positive.")
    require(args.dir.is_absolute() and not args.dir.is_symlink(), "Backup directory must be absolute and not a symlink.")
    if args.list:
        require(args.dir.is_dir(), "Backup directory does not exist.")
        for path in sorted(args.dir.glob("*.dump")):
            if path.is_file() and not path.is_symlink():
                print(f"{path.name} {path.stat().st_size} bytes")
        return
    for key in ("DB_NAME", "DB_PORT"):
        require(not placeholder(values.get(key)), f"Set a real {key} in .env.")
    args.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(args.dir.is_dir() and not args.dir.is_symlink(), "Backup directory changed unexpectedly.")
    os.chmod(args.dir, 0o700)
    name = f"{SERVICE}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{args.label}.dump"
    target = args.dir / name
    require(not target.exists(), "A backup with this timestamp and label already exists.")
    fd, partial = tempfile.mkstemp(prefix=".django-backup-", dir=args.dir)
    try:
        with os.fdopen(fd, "wb") as output:
            subprocess.run(["runuser", "-u", "postgres", "--", "pg_dump", "--format=custom",
                            "--no-owner", "--no-privileges", "--port", values["DB_PORT"],
                            "--dbname", values["DB_NAME"]],
                           stdout=output, check=True)
        with open(partial, "rb") as stream:
            require(stream.read(5) == b"PGDMP", "pg_dump did not produce a valid custom archive.")
        os.replace(partial, target)
    finally:
        if os.path.exists(partial):
            os.unlink(partial)
    print(f"Backup saved: {target}")
    backups = sorted(path for path in args.dir.glob(f"{SERVICE}-????????T??????Z-*.dump") if path.is_file() and not path.is_symlink())
    for old in backups[:-args.keep]:
        old.unlink()
        print(f"Removed old backup: {old.name}")


def main():
    command = sys.argv[1]
    if command == "environment":
        environment()
    elif command == "environment-check":
        validate_environment(read_env())
    elif command == "postgres":
        postgres()
    elif command == "database-check":
        database_check()
    elif command == "redis-check":
        redis_check()
    elif command == "redis-config":
        redis_config()
    elif command == "permissions":
        permissions()
    elif command in {"render", "render-check"}:
        render(check=command == "render-check")
    elif command == "static-check":
        static_check()
    elif command == "nginx-access":
        nginx_access()
    elif command == "superuser":
        superuser()
    elif command == "superuser-check":
        load_django()
        from django.contrib.auth import get_user_model
        require(get_user_model().objects.filter(is_superuser=True, is_staff=True, is_active=True).exists(),
                "No active staff superuser exists; rerun setup to create one.")
        print("Active staff superuser exists.")
    elif command == "health":
        health()
    elif command == "backup":
        backup()
    elif command == "manage":
        load_django()
        from django.core.management import execute_from_command_line
        execute_from_command_line([str(ROOT / "manage.py"), *sys.argv[2:]])
    elif command == "serve":
        load_django()
        os.execv(str(ROOT / ".venv-native/bin/gunicorn"), ["gunicorn", *sys.argv[2:]])
    else:
        raise ValueError("Unknown native deployment operation.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Driver/model exceptions can include submitted values. Never log those.
        if isinstance(exc, FileNotFoundError):
            print(f"ERROR: missing file or command: {exc.filename}", file=sys.stderr)
        elif isinstance(exc, ModuleNotFoundError):
            print(f"ERROR: missing Python module: {exc.name}", file=sys.stderr)
        else:
            print(f"ERROR: {exc}" if type(exc) is ValueError else f"ERROR: {type(exc).__name__}; operation failed. Check configuration and server logs.", file=sys.stderr)
        sys.exit(1)
