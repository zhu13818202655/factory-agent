#!/usr/bin/env bash

set -euo pipefail

command -v curl >/dev/null
command -v docker >/dev/null

export FACTORY_AGENT_PORT=0
export MOCK_MES_PORT=0
export USAGE_ADMIN_PORT=0
export POSTGRES_PORT=0
export REDIS_PORT=0

readonly project_name="factory-agent-image-test-$$"
compose=(
    docker compose
    --project-name "$project_name"
    -f deploy/compose/compose.yaml
)

cleanup() {
    "${compose[@]}" down --volumes --remove-orphans
}
trap cleanup EXIT

"${compose[@]}" up -d --no-build --wait --wait-timeout 30

test "$("${compose[@]}" exec -T agent-api id -u)" = "10001"
test "$("${compose[@]}" exec -T mock-mes id -u)" = "10001"
test "$("${compose[@]}" exec -T usage-admin id -u)" = "10001"

agent_endpoint="$("${compose[@]}" port agent-api 8000)"
mock_endpoint="$("${compose[@]}" port mock-mes 8010)"
usage_admin_endpoint="$("${compose[@]}" port usage-admin 8020)"

curl --fail --silent --show-error "http://127.0.0.1:${agent_endpoint##*:}/health/live"
printf '\n'
curl --fail --silent --show-error "http://127.0.0.1:${mock_endpoint##*:}/health/ready"
printf '\n'
curl --fail --silent --show-error "http://127.0.0.1:${usage_admin_endpoint##*:}/health/live"
printf '\n'
