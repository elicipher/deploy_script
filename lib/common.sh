#!/usr/bin/env bash
# shellcheck shell=bash
DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd -- "$DEPLOY_DIR/.." && pwd -P)"
ENV_FILE="$ROOT/.env"
VENV="$ROOT/.venv-native"
export ROOT ENV_FILE VENV
source "$DEPLOY_DIR/lib/system.sh"
source "$DEPLOY_DIR/lib/services.sh"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

initialize() {
    MODE="$1"
    STEP_NUMBER=0
    TOTAL_STEPS=13
    if [[ "$MODE" == update ]]; then TOTAL_STEPS=7; fi
    [[ $EUID == 0 ]] || die 'Run with sudo bash deploy-portable/setup.sh (or update.sh).'
    [[ -t 0 && -t 1 ]] || die 'An interactive terminal is required.'
    [[ "$ROOT" =~ ^/[a-zA-Z0-9/_-]+$ ]] || die 'Use a repository path with letters, digits, /, _ and - only.'
    command -v python3 >/dev/null || die 'Ubuntu python3 is required to bootstrap.'
    local instance identity name
    instance="$(printf '%s' "$ROOT" | sha256sum | cut -c1-16)"
    identity="/var/lib/django-deploy/instances/$instance"
    install -d -m 700 /var/lib/django-deploy /var/lib/django-deploy/instances
    exec 9>/var/lib/django-deploy/deploy.lock
    flock -n 9 || die 'Another native setup/update is running on this host.'
    [[ ! -L "$identity" ]] || die 'Service identity must not be a symlink.'
    if [[ -f "$identity" ]]; then
        IFS= read -r SERVICE < "$identity"
    else
        [[ "$MODE" == setup ]] || die 'No service identity found; run setup first.'
        printf 'Service name (lowercase letters, digits, hyphens): ' > /dev/tty
        IFS= read -r name < /dev/tty || die 'Service name input ended.'
        [[ "$name" =~ ^[a-z][a-z0-9-]{0,31}$ && "$name" != *- ]] || die 'Invalid service name.'
        [[ ! -e "/var/lib/$name" && ! -e "/etc/systemd/system/$name.service" && ! -e "/etc/nginx/sites-available/$name" ]] || die 'Service name is already in use.'
        systemctl cat "$name.service" >/dev/null 2>&1 && die 'Service name is already a systemd unit.'
        getent passwd "$name" >/dev/null && die 'Service name is already an OS user.'
        SERVICE="$name"
        printf '%s\n' "$SERVICE" > "$identity.tmp"
        chmod 600 "$identity.tmp"
        mv -- "$identity.tmp" "$identity"
    fi
    [[ "$SERVICE" =~ ^[a-z][a-z0-9-]{0,31}$ && "$SERVICE" != *- ]] || die 'Stored service name is invalid.'
    APP_USER="$SERVICE"
    STATE="/var/lib/$SERVICE"
    export SERVICE APP_USER STATE
    [[ ! -L "$STATE" ]] || die 'State directory must not be a symlink.'
    install -d -m 700 "$STATE" "$STATE/steps" "$STATE/logs"
    LOG="$STATE/logs/$(date -u +%Y%m%dT%H%M%S)-$$-$MODE.log"
    exec > >(python3 -u "$DEPLOY_DIR/lib/log.py" "$ENV_FILE" "$LOG") 2>&1
    LOGGER_PID=$!
    STEP=initialization
    trap 'failed "$?"' ERR
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'finish "$?"' EXIT
    cd -- "$ROOT"
    # The repository owner supplies Git credentials and builds dependencies.
    OWNER="$(stat -c %U "$ROOT")"
    [[ "$OWNER" != root ]] || die 'Clone as a non-root deployment administrator, then run with sudo.'
    export OWNER
    printf 'Native %s: %s\nPersistent log: %s\n' "$MODE" "$ROOT" "$LOG"
}

failed() {
    local code="$1"
    trap - ERR
    printf 'FAILED: %s (exit %s). Resume by rerunning the same script.\n' "$STEP" "$code" >&2
    printf 'failed\n' > "$STATE/steps/$MODE-$STEP.tmp"
    mv -- "$STATE/steps/$MODE-$STEP.tmp" "$STATE/steps/$MODE-$STEP"
    exit "$code"
}

finish() {
    local code="$1"
    trap - EXIT
    if [[ "$code" != 0 ]]; then
        printf 'failed\n' > "$STATE/steps/$MODE-$STEP.tmp"
        mv -- "$STATE/steps/$MODE-$STEP.tmp" "$STATE/steps/$MODE-$STEP"
    fi
    printf 'Finished with exit %s. Last step: %s. Log: %s\n' "$code" "$STEP" "$LOG"
    exec 1>&- 2>&-
    wait "$LOGGER_PID" || :
    exit "$code"
}

fingerprint() {
    { git_as rev-parse HEAD && sha256sum "$ENV_FILE" "$ROOT/requirements.txt" &&
      find "$DEPLOY_DIR" -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum;
    } | sha256sum | cut -d' ' -f1
}

run_step() {
    local id="$1" label="$2" prerequisite="$3" action="$4" verify="$5" stamp expected prefix
    STEP="$id"
    STEP_NUMBER=$((STEP_NUMBER + 1))
    prefix="$STEP_NUMBER/$TOTAL_STEPS:$id"
    printf '\n[%s] %s: validating prerequisites\n' "$prefix" "$label"
    "$prerequisite"
    stamp="$STATE/steps/$MODE-$id"
    # Bootstrap steps have no environment yet; their real-state checks are authoritative.
    expected=bootstrap
    if [[ -f "$ENV_FILE" && -x "$VENV/bin/python" ]]; then expected="$(fingerprint)"; fi
    if [[ -f "$stamp" && "$(cat "$stamp")" == "$expected" ]] && "$verify"; then
        printf '[%s] SKIPPED (verified)\n' "$prefix"
        return
    fi
    printf 'running\n' > "$stamp.tmp"
    mv -- "$stamp.tmp" "$stamp"
    printf '[%s] RUNNING\n' "$prefix"
    # Do not call actions inside an if: that disables Bash errexit inside functions.
    "$action"
    "$verify"
    if [[ -f "$ENV_FILE" && -x "$VENV/bin/python" ]]; then expected="$(fingerprint)"; fi
    printf '%s\n' "$expected" > "$stamp.tmp"
    mv -- "$stamp.tmp" "$stamp"
    printf '[%s] SUCCESS\n' "$prefix"
}

git_as() { runuser -u "$OWNER" -- git -C "$ROOT" "$@"; }
app() { runuser -u "$APP_USER" -- env -i HOME=/nonexistent SERVICE="$SERVICE" APP_USER="$APP_USER" STATE="$STATE" PATH="$VENV/bin:/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 "$@"; }
native() { app "$VENV/bin/python" "$DEPLOY_DIR/lib/native.py" "$@"; }
manage() { native manage "$@"; }
configure_environment() { root_native environment; }
environment_ready() { native environment-check; }
provision_postgres() {
    systemctl enable --now postgresql
    root_native postgres
}
postgres_ready() { systemctl is-enabled --quiet postgresql && systemctl is-active --quiet postgresql && native database-check; }
redis_ready() { systemctl is-enabled --quiet redis-server && systemctl is-active --quiet redis-server && native redis-check; }
dependencies_ready() { environment_ready && postgres_ready && redis_ready; }
django_checks() {
    dependencies_ready && manage check && manage check --deploy --fail-level ERROR
}
migrations_ready() { [[ ! -f "$STATE/migration-in-progress" ]] && manage migrate --check; }
migrate() {
    if [[ -f "$STATE/migration-in-progress" ]]; then
        die "A migration was interrupted. Review the migration plan, database and backup; see deploy-portable/README.md before removing $STATE/migration-in-progress."
    fi
    if manage migrate --check; then return; fi
    manage migrate --plan
    printf 'Back up existing databases before proceeding; migrations can change or remove data.\n'
    local answer
    printf 'Reviewed the plan and backed up existing data? Type y and press Enter to continue [y/N]:\n'
    read -r answer </dev/tty
    [[ "$answer" == y ]] || die 'Migration step cancelled.'
    touch "$STATE/migration-in-progress"
    manage migrate --noinput
    manage migrate --check
    rm -- "$STATE/migration-in-progress"
}
collect_static() { root_native permissions; manage collectstatic --noinput; root_native permissions; }
static_ready() { native static-check && root_native nginx-access; }
create_superuser() { native superuser; }
superuser_ready() { native superuser-check; }

git_clean() {
    local status
    status="$(git_as status --porcelain --untracked-files=normal)" || return
    [[ -z "$status" ]]
}

update_code() {
    STEP=git
    local branch upstream worktree
    worktree="$(git_as status --porcelain --untracked-files=normal)"
    [[ -z "$worktree" ]] || die 'Commit/stash local changes before updating (untracked files included).'
    branch="$(git_as symbolic-ref --quiet --short HEAD)"
    upstream="$(git_as rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')"
    [[ "$branch" == "$(cat "$STATE/branch")" && "$upstream" == "$(cat "$STATE/upstream")" ]] || die 'Branch/upstream changed since setup. Review before updating.'
    git_as fetch --quiet
    git_as merge --ff-only --quiet '@{upstream}'
    # This process already loaded the old shell helpers: never mix old and new deploy code.
    local current
    current="$(find "$DEPLOY_DIR" -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum)"
    if [[ "$current" != "$DEPLOY_HASH" ]]; then
        printf 'Deployment code changed. Rerun update.sh to load the new scripts.\n'
        exit 75
    fi
    refresh_code_access
}
DEPLOY_HASH="$(find "$DEPLOY_DIR" -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum)"
