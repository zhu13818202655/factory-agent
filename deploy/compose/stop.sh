#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

target="${1:-all}"

case "$target" in
    all)
        docker compose -f compose.yaml down --remove-orphans
        ;;
    middleware)
        docker compose -f middleware.yaml down --remove-orphans
        ;;
    *)
        printf 'Usage: %s [all|middleware]\n' "$(basename "$0")" >&2
        exit 64
        ;;
esac
