#!/usr/bin/env bash
#
# Destroy the local Docker data volumes and bring the stack back up empty.
#
# The PostgreSQL data directory is a named volume, so ``down`` alone keeps the
# data. Only deleting the volume makes ``postgres/init-databases.sql`` run again
# on the next start, which is what gives you a factory-fresh database (empty
# tables, no migration state).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

START_AFTER_RESET=1
ASSUME_YES=0

usage() {
    printf 'Usage: %s [all|middleware] [--no-start] [-y|--yes]\n' "$(basename "$0")" >&2
    printf '  all        Wipe the complete local stack volumes (PostgreSQL + Redis) and restart it.\n' >&2
    printf '  middleware Wipe the debugging middleware volumes (PostgreSQL + Redis) and restart it.\n' >&2
    printf '  --no-start Destroy the volumes and leave everything stopped.\n' >&2
    printf '  -y, --yes  Skip the confirmation prompt (for scripted runs).\n' >&2
}

choose_target() {
    printf '请选择要清库的目标：\n' >&2
    printf '  1) all        清空完整本地栈的数据卷：PostgreSQL、Redis（并重启整栈）\n' >&2
    printf '  2) middleware 只清空调试中间件的数据卷：PostgreSQL、Redis\n' >&2
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

target=""
while (($# > 0)); do
    case "$1" in
        all | middleware)
            target="$1"
            ;;
        --no-start)
            START_AFTER_RESET=0
            ;;
        -y | --yes)
            ASSUME_YES=1
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            printf '未知参数: %s\n' "$1" >&2
            usage
            exit 64
            ;;
    esac
    shift
done

if [[ -z "$target" ]]; then
    if [[ ! -t 0 ]]; then
        printf '未指定目标，且当前没有可用终端，请显式传入 all 或 middleware。\n' >&2
        exit 64
    fi
    target="$(choose_target)"
fi

case "$target" in
    all)
        COMPOSE_FILE="compose.yaml"
        PROJECT_NAME="factory-agent"
        ;;
    middleware)
        COMPOSE_FILE="middleware.yaml"
        PROJECT_NAME="factory-agent-middleware"
        ;;
esac

# Volumes the wipe will destroy, resolved from the live Compose project so the
# printout never claims to delete something that is not there. Written without
# `mapfile` because macOS still ships bash 3.2.
volumes=()
while IFS= read -r volume_name; do
    [[ -n "$volume_name" ]] && volumes+=("$volume_name")
done < <(docker volume ls --filter "name=${PROJECT_NAME}_" --format '{{.Name}}' | sort)

printf '即将销毁以下数据卷（%s）：\n' "$PROJECT_NAME" >&2
if ((${#volumes[@]} == 0)); then
    printf '  （当前没有该项目的持久化数据卷，等同于无数据状态）\n' >&2
else
    printf '  %s\n' "${volumes[@]}" >&2
fi
printf '容器内所有 PostgreSQL 数据库、迁移状态与 Redis AOF 都会被永久删除。\n' >&2

if ((ASSUME_YES == 0)); then
    if [[ ! -t 0 ]]; then
        printf '没有可用终端，无法交互确认；确认后请加 -y/--yes。\n' >&2
        exit 64
    fi
    printf '确认请输入 yes： ' >&2
    read -r answer
    if [[ "$answer" != "yes" ]]; then
        printf '已取消，未做任何改动。\n' >&2
        exit 1
    fi
fi

docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans

if ((START_AFTER_RESET == 1)); then
    if [[ "$target" == "all" ]]; then
        docker compose -f "$COMPOSE_FILE" up -d --build --wait
    else
        docker compose -f "$COMPOSE_FILE" up -d --wait
    fi
    printf '已清空 %s 的数据并重新启动。PostgreSQL 的 init-databases.sql 会重新执行。\n' \
        "$PROJECT_NAME" >&2
else
    printf '已清空 %s 的数据并停止所有相关容器（未启动）。\n' "$PROJECT_NAME" >&2
fi
