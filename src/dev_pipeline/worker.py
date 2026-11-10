"""Local worker entrypoint. Config-root is required and validated before connecting."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from dev_pipeline.activities import STUB_ACTIVITIES
from dev_pipeline.config_root import validate_config_root_path
from dev_pipeline.constants import DEFAULT_TASK_QUEUE, DEFAULT_TEMPORAL_TARGET
from dev_pipeline.errors import ConfigRootError
from dev_pipeline.workflows import WORKFLOWS

_MISSING_CONFIG_ROOT = (
    "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 --config-root。"
)


@dataclass(frozen=True)
class WorkerArgs:
    config_root: str
    temporal_target: str
    task_queue: str


def parse_worker_args(argv: list[str] | None = None) -> WorkerArgs:
    """Parse CLI arguments. --config-root has no default and is required."""

    parser = argparse.ArgumentParser(
        prog="dev-pipeline-worker",
        description="启动 dev-pipeline Temporal Worker。必须显式提供已存在的配置根目录。",
    )
    parser.add_argument(
        "--config-root",
        default=None,
        help="配置根目录（绝对路径，必须已存在；不会回退到当前目录或环境变量）",
    )
    parser.add_argument(
        "--temporal-target",
        default=DEFAULT_TEMPORAL_TARGET,
        help=f"Temporal 地址（默认 {DEFAULT_TEMPORAL_TARGET}）",
    )
    parser.add_argument(
        "--task-queue",
        default=DEFAULT_TASK_QUEUE,
        help=f"Task queue 名称（默认 {DEFAULT_TASK_QUEUE}）",
    )
    args = parser.parse_args(argv)
    if args.config_root is None or not str(args.config_root).strip():
        parser.error(_MISSING_CONFIG_ROOT)
    return WorkerArgs(
        config_root=str(args.config_root).strip(),
        temporal_target=str(args.temporal_target),
        task_queue=str(args.task_queue),
    )


async def run_worker(args: WorkerArgs) -> None:
    """Validate config-root first, then connect to Temporal and run the worker."""

    # Imported lazily so unit tests can parse args without a Temporal runtime.
    from temporalio.client import Client
    from temporalio.worker import Worker

    config_root = validate_config_root_path(args.config_root)
    client = await Client.connect(args.temporal_target)
    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=list(WORKFLOWS),
        activities=list(STUB_ACTIVITIES),
    )
    print(
        f"dev-pipeline worker 已连接 {args.temporal_target}，"
        f"task_queue={args.task_queue}，config_root={config_root}",
        flush=True,
    )
    await worker.run()


def main(argv: list[str] | None = None) -> None:
    try:
        args = parse_worker_args(argv)
        asyncio.run(run_worker(args))
    except ConfigRootError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
