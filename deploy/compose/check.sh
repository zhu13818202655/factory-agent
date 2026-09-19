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

case "$target" in
    all)
        check_all
        ;;
    middleware)
        check_middleware
        ;;
    *)
        printf 'Usage: %s [all|middleware]\n' "$(basename "$0")" >&2
        exit 64
        ;;
esac
