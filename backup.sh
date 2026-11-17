#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[[ $EUID == 0 ]] || { printf 'Run with sudo.\n' >&2; exit 1; }
IDENTITY="/var/lib/django-deploy/instances/$(printf '%s' "$ROOT" | sha256sum | cut -c1-16)"
[[ -f "$IDENTITY" && ! -L "$IDENTITY" && -x "$ROOT/.venv-native/bin/python" ]] || { printf 'Run deploy-portable/setup.sh first or repair its identity file.\n' >&2; exit 1; }
IFS= read -r SERVICE < "$IDENTITY"
[[ "$SERVICE" =~ ^[a-z][a-z0-9-]{0,31}$ && "$SERVICE" != *- ]] || { printf 'Invalid stored service name.\n' >&2; exit 1; }
export SERVICE
exec "$ROOT/.venv-native/bin/python" "$DEPLOY_DIR/lib/native.py" backup "$@"
