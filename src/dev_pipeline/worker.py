"""Local worker entrypoint. Config-root is required and validated before connecting."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import structlog

from dev_pipeline.activities import STUB_ACTIVITIES, ReopenStarter
from dev_pipeline.config_root import validate_config_root_path
from dev_pipeline.constants import (
    CLIENT_CONNECT_TIMEOUT,
    DEFAULT_TASK_QUEUE,
    DEFAULT_TEMPORAL_TARGET,
)
from dev_pipeline.errors import ConfigRootError
from dev_pipeline.workflows import WORKFLOWS

logger = structlog.get_logger("dev_pipeline.worker")

_MISSING_CONFIG_ROOT = (
    "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 --config-root。"
)


@dataclass(frozen=True)
class WorkerArgs:
    """CLI arguments for the local Temporal worker process.

    Attributes:
        config_root: Existing absolute directory for pipeline config.
        temporal_target: Temporal server address.
        task_queue: Task queue the worker polls.
    """

    config_root: str
    temporal_target: str
    task_queue: str


def parse_worker_args(argv: list[str] | None = None) -> WorkerArgs:
    """Parse CLI arguments. --config-root has no default and is required.

    Args:
        argv: Optional CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed worker arguments with a non-empty config-root string.
    """

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
    """Validate config-root first, then connect to Temporal and run the worker.

    Args:
        args: Parsed CLI arguments, including a required config-root path.
    """

    # Imported lazily so unit tests can parse args without a Temporal runtime.
    from temporalio.client import Client
    from temporalio.worker import Worker

    config_root = validate_config_root_path(args.config_root)
    client = await Client.connect(
        args.temporal_target,
        connect_timeout=CLIENT_CONNECT_TIMEOUT,
    )
    starter = ReopenStarter(client)
    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=list(WORKFLOWS),
        activities=[*STUB_ACTIVITIES, starter.start_reopened_workflow],
    )
    logger.info(
        "dev-pipeline worker 已连接",
        temporal_target=args.temporal_target,
        task_queue=args.task_queue,
        config_root=config_root,
    )
    await worker.run()


def main(argv: list[str] | None = None) -> None:
    """Start the worker, or exit with code 2 if config-root is invalid.

    Args:
        argv: Optional CLI arguments; defaults to ``sys.argv[1:]``.
    """

    try:
        args = parse_worker_args(argv)
        asyncio.run(run_worker(args))
    except ConfigRootError as exc:
        logger.error(str(exc))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
