#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

target="${1:-all}"

check_all() {
    docker compose -f compose.yaml config --quiet
    docker compose -f compose.yaml ps
}

check_middleware() {
    docker compose -f middleware.yaml config --quiet
    docker compose -f middleware.yaml ps
}

check_template() {
    # compose.yaml.template is the production-facing reference menu, not a stack
    # anyone runs as-is, so its ${VAR:?} placeholders are filled with throwaway
    # values here. This validates YAML syntax, anchors, and variable
    # interpolation without requiring a filled-in .env.
    local placeholders=(
        "FACTORY_AGENT_CANONICAL_MES_BASE_URL=http://template.invalid:9002"
        "FACTORY_AGENT_POSTGRES_URL=postgresql://factory_agent:placeholder@postgres:5432/factory_agent"
        "FACTORY_AGENT_S3_ACCESS_KEY=placeholder"
        "FACTORY_AGENT_S3_SECRET_KEY=placeholder"
        "POSTGRES_PASSWORD=placeholder"
        "USAGE_ADMIN_DATABASE_URL=postgresql://usage_admin:placeholder@postgres:5432/factory_agent"
        "USAGE_ADMIN_API_TOKEN=placeholder"
        "USAGE_ADMIN_TOKEN_SIGNING_SECRET=placeholder"
        "USAGE_ADMIN_EXPORT_SIGNING_SECRET=placeholder"
        "USAGE_ADMIN_DOWNLOAD_BASE_URL=http://template.invalid:8020"
        "USAGE_ADMIN_S3_ACCESS_KEY=placeholder"
        "USAGE_ADMIN_S3_SECRET_KEY=placeholder"
    )
    env "${placeholders[@]}" docker compose -f compose.yaml.template config --quiet
}

case "$target" in
    all)
        check_all
        ;;
    middleware)
        check_middleware
        ;;
    template)
        check_template
        ;;
    *)
        printf 'Usage: %s [all|middleware|template]\n' "$(basename "$0")" >&2
        exit 64
        ;;
esac
