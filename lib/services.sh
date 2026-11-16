#!/usr/bin/env bash
# shellcheck shell=bash
root_native() { env -i ROOT="$ROOT" ENV_FILE="$ENV_FILE" VENV="$VENV" SERVICE="$SERVICE" APP_USER="$APP_USER" STATE="$STATE" PATH=/usr/sbin:/usr/bin:/sbin:/bin "$VENV/bin/python" "$DEPLOY_DIR/lib/native.py" "$@"; }

configure_services() {
    root_native permissions
    root_native render
    systemd-analyze verify "/etc/systemd/system/$SERVICE.service"
    if celery_enabled; then systemd-analyze verify "/etc/systemd/system/$SERVICE-celery.service"; fi
    nginx -t
    systemctl daemon-reload
}

services_ready() {
    root_native render-check && systemd-analyze verify "/etc/systemd/system/$SERVICE.service" && nginx -t &&
        { ! celery_enabled || systemd-analyze verify "/etc/systemd/system/$SERVICE-celery.service"; }
}

start_services() {
    systemctl enable "$SERVICE" nginx
    systemctl restart "$SERVICE"
    if celery_enabled; then
        systemctl enable "$SERVICE-celery"
        systemctl restart "$SERVICE-celery"
    fi
    systemctl start nginx
    nginx -t
    systemctl reload nginx
    local attempt
    for attempt in {1..30}; do
        if health_ready; then return; fi
        sleep 2
    done
    die "Health check failed. Inspect journalctl -u $SERVICE; no automatic migration rollback is possible."
}

health_ready() {
    systemctl is-enabled --quiet "$SERVICE" && systemctl is-active --quiet "$SERVICE" &&
        systemctl is-enabled --quiet nginx && systemctl is-active --quiet nginx && native health &&
        { ! celery_enabled || { systemctl is-enabled --quiet "$SERVICE-celery" && systemctl is-active --quiet "$SERVICE-celery"; }; }
}
