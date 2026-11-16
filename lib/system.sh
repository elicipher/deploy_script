#!/usr/bin/env bash
# shellcheck shell=bash
PACKAGES=(python3.12 python3.12-venv python3-dev python3-dotenv python3-psycopg
    build-essential libpq-dev libjpeg-dev zlib1g-dev libwebp-dev libffi-dev libssl-dev
    postgresql postgresql-client nginx git curl ca-certificates acl)

validate_system() {
    [[ $EUID == 0 ]] || die 'Root privileges required.'
    # /etc/os-release is a system-owned shell data file, unlike the project .env.
    local ID VERSION_ID
    source /etc/os-release
    [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die 'Supported platform: Ubuntu 24.04 only.'
    [[ -d /run/systemd/system ]] || die 'A running systemd host is required.'
    [[ "$(df -Pk "$ROOT" | awk 'END {print $4}')" -ge 2097152 ]] || die 'At least 2 GiB free disk space required.'
    local file
    for file in manage.py requirements.txt; do
        [[ -f "$ROOT/$file" ]] || die "Missing project file: $file"
    done
    [[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE; create it before deploying."
    [[ ! -L "$ENV_FILE" && ! -L "$VENV" ]] || die '.env and .venv-native must not be symlinks.'
    [[ "$(realpath -e -- "$ROOT")" == "$ROOT" ]] || die 'Checkout path must not contain symlinks.'
    [[ -z "$(git_as ls-files -- .env .venv-native)" ]] || die 'Credentials/virtualenv must not be Git tracked.'
    git_as symbolic-ref --quiet --short HEAD > /dev/null
    git_as rev-parse --verify '@{upstream}' > /dev/null
    printf 'Ubuntu 24.04; architecture %s; systemd available; repository valid.\n' "$(dpkg --print-architecture)"
}

packages_ready() {
    local package
    for package in "${PACKAGES[@]}"; do
        [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null)" == 'install ok installed' ]] || return 1
    done
}

install_packages() {
    local package missing=()
    for package in "${PACKAGES[@]}"; do
        if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || :)" != 'install ok installed' ]]; then missing+=("$package"); fi
    done
    if ((${#missing[@]})); then
        printf 'Installing missing packages: %s\n' "${missing[*]}"
        # apt reports DNS/network/repository failures and stops; no blind retries.
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
}

create_identity() {
    if ! getent passwd "$APP_USER" >/dev/null; then
        useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
        touch "$STATE/identity-created"
    fi
    [[ -f "$STATE/identity-created" ]] || die 'Runtime username already exists and was not created by this deployment.'
    refresh_code_access
    git_as symbolic-ref --short HEAD > "$STATE/branch"
    git_as rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' > "$STATE/upstream"
}

refresh_code_access() {
    # Traversal only on ancestors; read-only code access, excluding secrets and uploads.
    local parent="$ROOT"
    while [[ "$parent" != / ]]; do setfacl -m "u:$APP_USER:--x,u:www-data:--x" "$parent"; parent="$(dirname -- "$parent")"; done
    find "$ROOT" -mindepth 1 \( -name .git -o -name .env -o -name .venv-native -o -name media -o -name staticfiles \) -prune -o -exec setfacl -m "u:$APP_USER:r-X" {} +
    setfacl -m "u:$APP_USER:r-x" "$ROOT"
}

identity_ready() {
    [[ -f "$STATE/identity-created" ]] && getent passwd "$APP_USER" >/dev/null &&
        [[ "$(getent passwd "$APP_USER" | cut -d: -f7)" == /usr/sbin/nologin ]] &&
        runuser -u "$APP_USER" -- test -r "$ROOT/config/wsgi.py"
}

install_python() {
    if python_ready; then
        printf 'Existing virtual environment and pinned requirements verified.\n'
        return
    fi
    if [[ ! -x "$VENV/bin/python" ]]; then runuser -u "$OWNER" -- python3.12 -m venv "$VENV"; fi
    "$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
    runuser -u "$OWNER" -- "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
    runuser -u "$OWNER" -- "$VENV/bin/python" -m pip check
    setfacl -R -m "u:$APP_USER:r-X" "$VENV"
    sha256sum "$ROOT/requirements.txt" > "$STATE/requirements.tmp"
    mv -- "$STATE/requirements.tmp" "$STATE/requirements"
}

python_ready() {
    [[ -f "$STATE/requirements" ]] && sha256sum --status -c "$STATE/requirements" &&
        app "$VENV/bin/python" -c 'import sys, django, gunicorn, dotenv, psycopg; assert sys.version_info[:2] == (3, 12)' &&
        app "$VENV/bin/python" -m pip check
}

provision_redis() {
    if [[ "$(dpkg-query -W -f='${Status}' redis-server 2>/dev/null || :)" != 'install ok installed' ]]; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends redis-server
    fi
    root_native redis-config
    systemctl enable --now redis-server
    if ! redis_ready; then systemctl restart redis-server; fi
}
