#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
    printf 'Usage: %s [all|middleware] [amd64|arm64]\n' "$(basename "$0")" >&2
    printf '  all        Start PostgreSQL, Redis, SeaweedFS (S3) and agent-api.\n' >&2
    printf '  middleware Start only local PostgreSQL, Redis and SeaweedFS (S3) for debugging.\n' >&2
    printf '  架构参数仅对 all 生效，默认 amd64（本机 x86）；arm64 用于给客户交叉构建镜像。\n' >&2
}

choose_target() {
    printf '请选择要启动的目标：\n' >&2
    printf '  1) all        启动完整本地栈：PostgreSQL、Redis、SeaweedFS(S3)、agent-api\n' >&2
    printf '  2) middleware 只启动本地调试中间件：PostgreSQL、Redis、SeaweedFS(S3)\n' >&2
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
        ARCH="${2:-amd64}"
        case "${ARCH}" in
            amd64|x86_64)
                PLATFORM="linux/amd64"
                BUILDER="${BUILDX_BUILDER:-default}"
                ;;
            arm64|aarch64)
                PLATFORM="linux/arm64"
                BUILDER="${BUILDX_BUILDER:-factory-agent-arm64}"
                if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
                    docker buildx create --name "${BUILDER}" --driver docker-container --driver-opt network=host
                fi
                ;;
            *)
                printf '无效架构: %s（可选 amd64/arm64，默认 amd64）\n' "${ARCH}" >&2
                exit 64
                ;;
        esac
        DOCKER_DEFAULT_PLATFORM="${PLATFORM}" BUILDX_BUILDER="${BUILDER}" \
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
