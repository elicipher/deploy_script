# Reusable Django deployment kit

Copy this entire `deploy-portable/` directory into the root of another Django
repository. You may rename the directory; replace `deploy-portable` in the
commands below with its actual name. It targets Ubuntu 24.04 with systemd, PostgreSQL 16, Nginx,
Python 3.12, and Gunicorn. The target repository must have `manage.py`,
`requirements.txt`, a Git branch with an upstream, and a production `.env`.
`requirements.txt` must install Django, Gunicorn, python-dotenv, psycopg, and
Redis/Celery Python packages when those features are configured. The Django settings must read the values below and configure the default
PostgreSQL database and static/media paths consistently. The deployment
checks that Django's default database matches the provisioned database.

Example `.env` values (replace every example and secret):

```dotenv
SECRET_KEY=replace-with-a-random-secret-at-least-50-characters-long
DEBUG=False
DEPLOY_SETTINGS_MODULE=myproject.settings
DEPLOY_WSGI_MODULE=myproject.wsgi
DEPLOY_HEALTH_PATH=/health/
APP_DOMAIN=api.example.com
ALLOWED_HOSTS=api.example.com
DB_NAME=myproject
DB_USER=myproject
DB_PASSWORD=replace-with-a-strong-database-password
DB_HOST=127.0.0.1
DB_PORT=5432
# Optional local Redis: REDIS_URL=redis://127.0.0.1:6379/0
# Optional Celery worker (set both):
# CELERY_BROKER_URL=redis://127.0.0.1:6379/1
# DEPLOY_CELERY_APP=myproject.celery
STATIC_ROOT=/var/www/myproject/static
MEDIA_ROOT=/srv/myproject/media
```

DEBUG may be set to True temporarily for troubleshooting; restore False afterward because Django exposes detailed error pages when it is enabled.

The health endpoint must return HTTP 200 without authentication, directly from
Gunicorn and through Nginx. Set `DEPLOY_HEALTH_JSON=True` only when it returns
JSON with `status`, `database`, and `cache` all equal to `ok`; this gives an
application-level database/cache readiness check. The default HTTP 200 check
only verifies the web path. If Django redirects HTTP to HTTPS, configure a real
certificate before relying on the HTTPS readiness check. Configure any
project-specific CORS, CSRF, proxy, cookie, SMS, and payment settings yourself.

Without `REDIS_URL` or a Celery broker pointing at local Redis, setup skips the
Redis package and service steps, and the Gunicorn unit does not depend on Redis.
With `CELERY_BROKER_URL` and `DEPLOY_CELERY_APP`, setup installs and starts a
dedicated Celery Worker unit named `<service>-celery`; both values must be set.
Add `celery` to the project's `requirements.txt` and `redis` too if the broker
uses Redis. With no Celery values, no Worker unit is created. Removing them on
an update stops and removes the managed Worker unit. Redis is a shared host
service and is not uninstalled or stopped when configuration is removed.
Packages already listed in `requirements.txt` are still installed by pip.
Beat is not configured. A remote Celery broker does not cause local Redis to be
installed. Give each project its own Celery broker database, virtual host or
queue so workers do not consume another project's tasks. Celery Worker logs
may include application data; restrict access.

From the target repository root, after creating `.env` and setting DNS:

```bash
sudo bash deploy-portable/setup.sh
sudo bash deploy-portable/service.sh status
sudo bash deploy-portable/service.sh logs
sudo bash deploy-portable/service.sh worker-status  # only with Celery
sudo bash deploy-portable/service.sh worker-logs    # only with Celery
sudo bash deploy-portable/backup.sh --label before-update
sudo bash deploy-portable/update.sh
```

Commit the copied deployment directory before using `update.sh`; it rejects
untracked or locally modified files.

Setup asks once for a service name. Use lowercase letters, digits and hyphens,
starting with a letter (maximum 32 characters). The exact name becomes the
systemd unit, dedicated OS user, Nginx site name and `/var/lib/<name>/` state
directory. Its mapping to the checkout path is stored in
`/var/lib/django-deploy/instances/`; later commands reuse it and do not ask
again. Keep the checkout at the same absolute path. To change the service name,
plan a manual migration of the unit, user, state and Nginx site; editing the
mapping alone is unsafe.

Setup installs packages, provisions the local database and configured Redis, validates
Django, asks for approval before migrations, collects static files, creates a
superuser if missing, configures Gunicorn/Nginx and the configured Worker, and checks readiness. Update
fetches/fast-forwards the configured Git branch and repeats changed deployment
steps. Backup writes a custom PostgreSQL dump with 14-file retention for this
service. Keep your `.env`, uploaded media and backups off-site as well.

Each checkout needs a distinct service name, domain, database name/user, and
static/media paths. Redis and the host-wide PostgreSQL, Nginx and apt services
are shared. Redis setup may append loopback-only settings and restart Redis;
review existing users of that service before setup. Updates are in place and do not roll back migrations.
Use a maintenance window and a verified backup. The scripts require a non-root
checkout owner, an interactive terminal, a path with only ASCII letters,
digits, `/`, `_`, and `-`, and a clean Git checkout before updates. Never commit
`.env`. Run on an authorized test VPS before production use.
