#!/usr/bin/env bash
# Generic (non-Windows) helper. Windows is the first documented environment;
# see scripts/start-worker.ps1 and docs/dev-setup.md.
set -euo pipefail

CONFIG_ROOT=""
TEMPORAL_TARGET="localhost:7233"
TASK_QUEUE="dev-pipeline"

usage() {
  echo "用法: $0 --config-root /absolute/path [--temporal-target localhost:7233] [--task-queue dev-pipeline]" >&2
  echo "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。" >&2
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

python -m dev_pipeline.worker \
  --config-root "${CONFIG_ROOT}" \
  --temporal-target "${TEMPORAL_TARGET}" \
  --task-queue "${TASK_QUEUE}"
