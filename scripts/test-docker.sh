#!/usr/bin/env bash
# Run the Docker test gate. Use --live-history to run live History tests
# against the official already-running Temporal. This script never starts
# a Temporal server.
set -euo pipefail

LIVE_HISTORY=0
if [[ "${1:-}" == "--live-history" ]]; then
  LIVE_HISTORY=1
elif [[ "${1:-}" == "--with-temporal" ]]; then
  echo "不要用 --with-temporal 再起一份 Temporal。本机正式服务已占用 127.0.0.1:7233（容器 agent-platform-temporal-1）。" >&2
  echo "请改用 --live-history，并把 TEMPORAL_TARGET 设为 host.docker.internal:7233。" >&2
  echo "不要使用 docker compose --profile temporal，也不要使用 temporal server start-dev。" >&2
  exit 2
elif [[ $# -gt 0 ]]; then
  echo "未知参数: $1" >&2
  echo "用法: $0 [--live-history]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if [[ "${LIVE_HISTORY}" -eq 1 ]]; then
  docker compose run --build --rm -e TEMPORAL_TARGET=host.docker.internal:7233 test python -m pytest -m temporal_server -vv
  exit 0
fi

docker compose run --build --rm test
