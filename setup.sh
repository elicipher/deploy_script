#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
initialize setup
run_step system 'Validate Ubuntu and repository' true validate_system validate_system
run_step packages 'Install missing native packages' validate_system install_packages packages_ready
run_step identity 'Create isolated runtime identity' packages_ready create_identity identity_ready
run_step python 'Install Python dependencies' identity_ready install_python python_ready
run_step environment 'Validate production .env' python_ready configure_environment environment_ready
run_step postgres 'Provision PostgreSQL safely' environment_ready provision_postgres postgres_ready
if redis_enabled; then
    run_step redis 'Restrict and start Redis' environment_ready provision_redis redis_ready
else
    skip_step redis
fi
run_step checks 'Validate Django and connections' dependencies_ready django_checks django_checks
run_step migrations 'Apply database migrations' django_checks migrate migrations_ready
run_step static 'Collect static files and permissions' migrations_ready collect_static static_ready
run_step superuser 'Create administrator account' migrations_ready create_superuser superuser_ready
run_step services 'Configure Gunicorn and Nginx' static_ready configure_services services_ready
run_step health 'Verify application through both servers' services_ready start_services health_ready
fingerprint > "$STATE/installed.tmp"
mv -- "$STATE/installed.tmp" "$STATE/installed"
printf 'Deployment complete. Service: %s; logs: %s\n' "$SERVICE" "$LOG"
