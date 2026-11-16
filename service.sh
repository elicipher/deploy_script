#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ $EUID == 0 ]] || { printf 'Run with sudo.\n' >&2; exit 1; }
IDENTITY="/var/lib/django-deploy/instances/$(printf '%s' "$ROOT" | sha256sum | cut -c1-16)"
[[ -f "$IDENTITY" && ! -L "$IDENTITY" ]] || { printf 'Run deploy-portable/setup.sh first or repair its identity file.\n' >&2; exit 1; }
IFS= read -r SERVICE < "$IDENTITY"
[[ "$SERVICE" =~ ^[a-z][a-z0-9-]{0,31}$ && "$SERVICE" != *- ]] || { printf 'Invalid stored service name.\n' >&2; exit 1; }

if (($# != 1)); then
    printf 'Usage: sudo bash deploy-portable/service.sh {status|restart|logs|worker-status|worker-restart|worker-logs}\n' >&2
    exit 2
fi

case "$1" in
    status) exec systemctl status "$SERVICE" --no-pager ;;
    restart) systemctl restart "$SERVICE"; exec systemctl status "$SERVICE" --no-pager ;;
    logs) exec journalctl -u "$SERVICE" -n 50 --no-pager ;;
    worker-status) exec systemctl status "$SERVICE-celery" --no-pager ;;
    worker-restart) systemctl restart "$SERVICE-celery"; exec systemctl status "$SERVICE-celery" --no-pager ;;
    worker-logs) exec journalctl -u "$SERVICE-celery" -n 50 --no-pager ;;
    *) printf 'Usage: sudo bash deploy-portable/service.sh {status|restart|logs|worker-status|worker-restart|worker-logs}\n' >&2; exit 2 ;;
esac
