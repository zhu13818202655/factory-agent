#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
    printf 'Usage: %s [all|middleware]\n' "$(basename "$0")" >&2
    printf '  all        Start PostgreSQL, Redis, agent-api, mock-mes, and usage-admin.\n' >&2
    printf '  middleware Start only local PostgreSQL and Redis for debugging.\n' >&2
}

choose_target() {
    printf '请选择要启动的目标：\n' >&2
    printf '  1) all        启动完整本地栈：PostgreSQL、Redis、agent-api、mock-mes、usage-admin\n' >&2
    printf '  2) middleware 只启动本地调试中间件：PostgreSQL、Redis\n' >&2
    printf '请输入 1/2 或 all/middleware: ' >&2
    read -r choice

    case "$choice" in
        1 | all)
            printf 'all\n'
            ;;
        2 | middleware)
            printf 'middleware\n'
            ;;
        *)
            printf '无效选择: %s\n' "$choice" >&2
            usage
            exit 64
            ;;
    esac
}

if (($# == 0)); then
    target="$(choose_target)"
else
    target="$1"
fi

case "$target" in
    all)
        docker compose -f compose.yaml up -d --build --wait
        ;;
    middleware)
        docker compose -f middleware.yaml up -d --wait
        ;;
    *)
        usage
        exit 64
        ;;
esac
