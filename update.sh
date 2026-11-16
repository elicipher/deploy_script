#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
initialize update
[[ -f "$STATE/installed" ]] || die 'Initial setup has not completed; run deploy-portable/setup.sh.'
validate_system
identity_ready || die 'Runtime identity missing; rerun setup.'
environment_ready || die 'Environment invalid; repair .env or rerun setup.'
packages_ready || die 'Native packages missing; rerun setup.'
# Fetch once per invocation. A dirty tree is always an error, even on resume.
rm -f -- "$STATE/steps/update-git"
run_step git 'Fetch and fast-forward the configured branch' environment_ready update_code git_clean
run_step python 'Install changed dependencies' identity_ready install_python python_ready
run_step checks 'Validate Django and connections' dependencies_ready django_checks django_checks
run_step migrations 'Apply new migrations' django_checks migrate migrations_ready
run_step static 'Collect updated static files' migrations_ready collect_static static_ready
run_step services 'Validate native service configuration' static_ready configure_services services_ready
run_step health 'Restart and verify the release' services_ready start_services health_ready
fingerprint > "$STATE/installed.tmp"
mv -- "$STATE/installed.tmp" "$STATE/installed"
printf 'Update complete. No automatic database rollback. Logs: %s\n' "$LOG"
