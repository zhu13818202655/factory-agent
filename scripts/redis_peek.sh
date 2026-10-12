#!/usr/bin/env bash
# 查看 Redis 里的缓存键与键内容（V2 结果集缓存运维用）。
#
# 宿主机的 redis-cli 不可用——Redis 跑在 middleware 容器里，所以统一走 docker exec。
# 键名约 130 字符、值最大 ~40MB，因此这里**从不整篇打印值**：要么只看元信息，
# 要么只解析出结构骨架（行数据默认不打，要看用 --rows）。
#
#   scripts/redis_peek.sh                    列出 fa:* 键（类型/TTL/字节数/内存）
#   scripts/redis_peek.sh --all              列出全部键，不限前缀
#   scripts/redis_peek.sh --pattern 'mes-*'  自定义键名前缀
#   scripts/redis_peek.sh 5610670a           按键名子串定位，打印值结构骨架
#   scripts/redis_peek.sh 5610670a --rows 3  骨架 + 前 3 行原始数据
#   scripts/redis_peek.sh --raw 5610670a     只看值的前 300 字节
#
# 环境变量：REDIS_CONTAINER（默认 factory-agent-middleware-redis-1）
#           REDIS_DB（默认 0，与 .env 的 redis://…/0 一致）
#           PYTHON（默认仓库 .venv/bin/python，退回 python3）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${REDIS_CONTAINER:-factory-agent-middleware-redis-1}"
DB="${REDIS_DB:-0}"
PATTERN="fa:*"

if [ -z "${PYTHON:-}" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then
        PYTHON="$ROOT/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

redis_cli() { docker exec "$CONTAINER" redis-cli -n "$DB" "$@"; }

# 按子串取第一个匹配键；取不到就报错退出。
find_key() {
    local needle="$1" key
    key="$(redis_cli --scan --pattern "$PATTERN" | grep -F -- "$needle" | head -n 1 || true)"
    if [ -z "$key" ]; then
        echo "没有键名含 '$needle' 的键（当前 DB ${DB}）" >&2
        echo "先跑 scripts/redis_peek.sh 看看有哪些键。" >&2
        exit 1
    fi
    printf '%s' "$key"
}

list_keys() {
    local keys total
    total="$(redis_cli DBSIZE)"
    keys="$(redis_cli --scan --pattern "$PATTERN" || true)"
    if [ -z "$keys" ]; then
        echo "DB ${DB}：共 $total 个键，但没有匹配 '$PATTERN' 的键。"
        return 0
    fi
    echo "DB ${DB}：共 $total 个键，匹配 '$PATTERN' 的有 $(printf '%s\n' "$keys" | wc -l | tr -d ' ') 个："
    echo
    while read -r key; do
        [ -z "$key" ] && continue
        printf '  type=%-7s ttl=%-8s bytes=%-10s mem=%-10s …%s\n' \
            "$(redis_cli TYPE "$key")" \
            "$(redis_cli TTL "$key")" \
            "$(redis_cli STRLEN "$key")" \
            "$(redis_cli MEMORY USAGE "$key")" \
            "${key: -28}"
    done <<< "$keys"
    echo
    echo "看某个键的内容： scripts/redis_peek.sh <键名子串，如末尾 8 位>"
}

DECODER="$(
    cat <<'PYEOF'
import json, os, sys

payload = json.load(sys.stdin)
wanted = int(os.environ.get("SHOW_ROWS", "0") or "0")
rows = payload.get("rows") or []

print(f"  值结构版本 v = {payload.get('v')}")
print(f"  接口     api = {payload.get('api')}")
print(f"  窗口  window = {payload.get('window')}")
print(f"  参数 filters = {payload.get('filters')}")
print(f"  分组 groupby = {payload.get('groupby')}   派生={payload.get('derived')}")
print(f"  列  columns  = {len(payload.get('columns') or [])} 列 {payload.get('columns')}")
print(f"  行     rows  = {len(rows)} 行")
print(f"  合计 footer  = {payload.get('footer')}")
print(f"  生成时间     = {payload.get('generated_at')}")
print(f"  来源 source  = {payload.get('source')}")
if wanted:
    print(f"  前 {wanted} 行：")
    for row in rows[:wanted]:
        print(f"    {row}")
PYEOF
)"

peek() {
    local needle="$1" show_rows="$2" key
    key="$(find_key "$needle")"
    printf '键: %s\n' "$key"
    printf 'TTL=%ss  bytes=%s\n\n' "$(redis_cli TTL "$key")" "$(redis_cli STRLEN "$key")"
    if [ "$(redis_cli TYPE "$key")" != "string" ]; then
        echo "该键不是 string 类型，跳过解码。" >&2
        return 0
    fi
    # 必须 export：右侧是管道，python 是子进程，只有导出的变量才在它的环境里。
    export SHOW_ROWS="$show_rows"
    redis_cli GET "$key" | "$PYTHON" -c "$DECODER"
}

raw_peek() {
    local needle="$1" key
    key="$(find_key "$needle")"
    printf '键: %s\n前 300 字节:\n' "$key"
    redis_cli GET "$key" | head -c 300
    printf '\n…(截断)\n'
}

MODE="list"
NEEDLE=""
SHOW_ROWS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --all) PATTERN="*" ;;
        --pattern) PATTERN="$2"; shift ;;
        --raw) MODE="raw"; NEEDLE="$2"; shift ;;
        --rows) SHOW_ROWS="$2"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) MODE="peek"; NEEDLE="$1" ;;
    esac
    shift
done

case "$MODE" in
    list) list_keys ;;
    raw) raw_peek "$NEEDLE" ;;
    peek) peek "$NEEDLE" "$SHOW_ROWS" ;;
esac
