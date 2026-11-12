#!/usr/bin/env bash
# Generic (non-Windows) helper. Windows is the first documented environment;
# see scripts/start-worker.ps1 and docs/dev-setup.md.
# This script never starts a Temporal server.
set -euo pipefail

CONFIG_ROOT=""
TEMPORAL_TARGET="127.0.0.1:7233"
TASK_QUEUE="dev-pipeline"
SKIP_TEMPORAL_CHECK=0

usage() {
  echo "用法: $0 --config-root /absolute/path [--temporal-target 127.0.0.1:7233] [--task-queue dev-pipeline]" >&2
  echo "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。" >&2
  echo "本仓库 Worker 默认连接已运行的官方 Temporal：127.0.0.1:7233（容器 agent-platform-temporal-1）。" >&2
  echo "本脚本不会启动 Temporal。不要使用 docker compose --profile temporal，也不要使用 temporal server start-dev。" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config-root)
      CONFIG_ROOT="${2:-}"
      shift 2
      ;;
    --temporal-target)
      TEMPORAL_TARGET="${2:-}"
      shift 2
      ;;
    --task-queue)
      TASK_QUEUE="${2:-}"
      shift 2
      ;;
    --skip-temporal-check)
      SKIP_TEMPORAL_CHECK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${CONFIG_ROOT}" ]]; then
  usage
  exit 2
fi

if [[ "${CONFIG_ROOT}" != /* ]]; then
  echo "配置根目录必须是绝对路径，已拒绝相对路径: ${CONFIG_ROOT}" >&2
  exit 2
fi
if [[ ! -e "${CONFIG_ROOT}" ]]; then
  echo "配置根目录不存在: ${CONFIG_ROOT}。不会当作“可能已存在”继续执行。" >&2
  exit 2
fi
if [[ ! -d "${CONFIG_ROOT}" ]]; then
  echo "配置根目录不是目录: ${CONFIG_ROOT}" >&2
  exit 2
fi

if [[ "${SKIP_TEMPORAL_CHECK}" -eq 0 && "${TEMPORAL_TARGET}" == "127.0.0.1:7233" ]]; then
  if ! (echo >/dev/tcp/127.0.0.1/7233) >/dev/null 2>&1; then
    echo "本仓库 Worker 连接到已运行的官方 Temporal：127.0.0.1:7233（容器 agent-platform-temporal-1）。" >&2
    echo "当前该地址没有服务。请启动官方 agent-platform 栈，不要在本仓库再起一份 Temporal，也不要使用 docker compose --profile temporal 或 temporal server start-dev。" >&2
    exit 1
  fi
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

python -m dev_pipeline.worker \
  --config-root "${CONFIG_ROOT}" \
  --temporal-target "${TEMPORAL_TARGET}" \
  --task-queue "${TASK_QUEUE}"
