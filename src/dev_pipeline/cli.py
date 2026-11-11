"""Command-line entry for starting one DevPipelineWorkflow into Intake."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NoReturn, Protocol

import structlog

from dev_pipeline.budget_config import load_budget_config_from_path
from dev_pipeline.config_root import validate_config_root_path
from dev_pipeline.constants import (
    CLIENT_CONNECT_TIMEOUT,
    DEFAULT_TASK_QUEUE,
    DEFAULT_TEMPORAL_TARGET,
)
from dev_pipeline.errors import BudgetConfigError, ConfigRootError, PipelineStartError
from dev_pipeline.intake import intake_from_cli, story_input_from_intake, validate_jira_key
from dev_pipeline.models import StoryInput, story_workflow_id

logger = structlog.get_logger("dev_pipeline.cli")

_MISSING_CONFIG_ROOT = (
    "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 --config-root。"
)
_MISSING_JIRA_KEY = (
    "Jira key 是必填参数，必须是 DP 项目的 issue key，例如 DP-13。请传入 --jira-key。"
)
_NEED_START = (
    "请使用子命令 start，例如: dev-pipeline start --jira-key DP-13 --config-root <绝对路径>"
)
_EMPTY_EXECUTION_ID = "execution_id 不能为空。省略该参数时会自动生成，传入时必须是非空值。"
_ALREADY_EXISTS = (
    "无法启动：目标执行已存在（workflow_id={workflow_id}）。"
    "这是一次失败，不会当作“可能已存在”而继续或接入已有执行。"
)
_CONNECT_FAILED = "无法连接 Temporal（{target}）: {exc}。启动已中止，不会当作已成功。"
_START_FAILED = (
    "启动 DevPipelineWorkflow 失败（workflow_id={workflow_id}）: {exc}。"
    "启动已中止，不会当作已成功。"
)
PARENT_EXECUTION_TIMEOUT = timedelta(hours=24)
PARENT_TASK_TIMEOUT = timedelta(seconds=30)
START_RPC_TIMEOUT = CLIENT_CONNECT_TIMEOUT


class WorkflowStarter(Protocol):
    """Minimal Temporal client surface used to start a parent execution."""

    def start_workflow(self, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        """Start a workflow and return a handle.

        ``Any`` matches Temporal ``Client.start_workflow`` and test fakes.
        """


class ConnectFn(Protocol):
    """Connect to Temporal with an explicit timeout."""

    def __call__(self, target: str, *, connect_timeout: timedelta) -> Awaitable[WorkflowStarter]:
        """Return a client for ``target``."""


@dataclass(frozen=True)
class StartArgs:
    """Parsed ``dev-pipeline start`` arguments."""

    command: str
    jira_key: str
    config_root: str
    execution_id: str | None
    temporal_target: str
    task_queue: str


@dataclass(frozen=True)
class StartResult:
    """Successful CLI start identifiers, printed for the operator."""

    workflow_id: str
    execution_id: str
    jira_key: str


def parse_cli_args(argv: list[str] | None = None) -> StartArgs:
    """Parse CLI arguments. ``start``, ``--jira-key`` and ``--config-root`` are required.

    Args:
        argv: Optional CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed start arguments. ``execution_id`` may be omitted.
    """

    parser = argparse.ArgumentParser(
        prog="dev-pipeline",
        description="用 CLI 显式启动一条 DevPipelineWorkflow，并进入 Intake。",
    )
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser(
        "start",
        help="根据 Jira key 启动父执行并进入 Intake。",
    )
    start.add_argument(
        "--jira-key",
        default=None,
        help="DP 项目的 Jira key，例如 DP-13。不把 Jira 当成编排器。",
    )
    start.add_argument(
        "--config-root",
        default=None,
        help="配置根目录（绝对路径，必须已存在；不会回退到当前目录或环境变量）",
    )
    start.add_argument(
        "--execution-id",
        default=None,
        help="本次执行 id；省略则生成 UUID 并打印。与 Jira key 一起构成 workflow id。",
    )
    start.add_argument(
        "--temporal-target",
        default=DEFAULT_TEMPORAL_TARGET,
        help=f"Temporal 地址（默认 {DEFAULT_TEMPORAL_TARGET}）",
    )
    start.add_argument(
        "--task-queue",
        default=DEFAULT_TASK_QUEUE,
        help=f"Task queue 名称（默认 {DEFAULT_TASK_QUEUE}）",
    )
    return _start_args_from_parse(parser, parser.parse_args(argv))


def _start_args_from_parse(parser: argparse.ArgumentParser, args: argparse.Namespace) -> StartArgs:
    """Validate required flags after argparse has parsed the command line."""

    if args.command != "start":
        parser.error(_NEED_START)
    if args.jira_key is None or not str(args.jira_key).strip():
        parser.error(_MISSING_JIRA_KEY)
    if args.config_root is None or not str(args.config_root).strip():
        parser.error(_MISSING_CONFIG_ROOT)
    execution_id = args.execution_id
    if execution_id is not None:
        execution_id = str(execution_id)
    return StartArgs(
        command="start",
        jira_key=str(args.jira_key).strip(),
        config_root=str(args.config_root).strip(),
        execution_id=execution_id,
        temporal_target=str(args.temporal_target),
        task_queue=str(args.task_queue),
    )


def _resolve_execution_id(raw: str | None) -> str:
    """Return a non-empty execution id, generating a UUID when omitted."""

    if raw is None:
        return uuid.uuid4().hex
    value = raw.strip()
    if not value:
        raise PipelineStartError(_EMPTY_EXECUTION_ID)
    return value


async def _default_connect(target: str, *, connect_timeout: timedelta) -> WorkflowStarter:
    """Connect to a Temporal server with an explicit timeout."""

    from temporalio.client import Client

    return await Client.connect(target, connect_timeout=connect_timeout)


async def _connect(
    target: str,
    connect: ConnectFn | None,
) -> WorkflowStarter:
    """Connect to Temporal, or fail visibly in Chinese."""

    opener = connect or _default_connect
    try:
        return await opener(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    except PipelineStartError:
        raise
    except Exception as exc:
        raise PipelineStartError(_CONNECT_FAILED.format(target=target, exc=exc)) from exc


async def _start_workflow(
    client: WorkflowStarter,
    payload: StoryInput,
    workflow_id: str,
    task_queue: str,
) -> None:
    """Start DevPipelineWorkflow, never attaching to an existing id as success."""

    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from dev_pipeline.workflows import DevPipelineWorkflow

    try:
        await client.start_workflow(
            DevPipelineWorkflow.run,
            payload,
            id=workflow_id,
            task_queue=task_queue,
            execution_timeout=PARENT_EXECUTION_TIMEOUT,
            task_timeout=PARENT_TASK_TIMEOUT,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            rpc_timeout=START_RPC_TIMEOUT,
        )
    except WorkflowAlreadyStartedError as exc:
        raise PipelineStartError(_ALREADY_EXISTS.format(workflow_id=workflow_id)) from exc
    except PipelineStartError:
        raise
    except Exception as exc:
        raise PipelineStartError(_START_FAILED.format(workflow_id=workflow_id, exc=exc)) from exc


async def run_start(args: StartArgs, *, connect: ConnectFn | None = None) -> StartResult:
    """Validate config, build Intake, and start one parent execution.

    Args:
        args: Parsed start arguments.
        connect: Optional Temporal connector; used by tests to inject fakes.

    Returns:
        Identifiers of the started execution.

    Raises:
        ConfigRootError: Config-root is missing or invalid.
        BudgetConfigError: Required budget.toml is missing or invalid.
        PipelineStartError: The Jira key, execution id, or Temporal start failed.
    """

    jira_key = validate_jira_key(args.jira_key)
    execution_id = _resolve_execution_id(args.execution_id)
    config_root = validate_config_root_path(args.config_root)
    load_budget_config_from_path(config_root)
    payload = story_input_from_intake(
        intake_from_cli(jira_key),
        config_root=config_root,
        execution_id=execution_id,
    )
    workflow_id = story_workflow_id(payload.story_id, payload.execution_id)
    client = await _connect(args.temporal_target, connect)
    await _start_workflow(client, payload, workflow_id, args.task_queue)
    logger.info(
        "已启动 DevPipelineWorkflow，已进入 Intake",
        workflow_id=workflow_id,
        execution_id=execution_id,
        jira_key=jira_key,
    )
    return StartResult(workflow_id=workflow_id, execution_id=execution_id, jira_key=jira_key)


def _format_success(result: StartResult) -> str:
    """Return the copyable success text for stdout."""

    return (
        "已启动 DevPipelineWorkflow，已进入 Intake。\n"
        f"workflow_id={result.workflow_id}\n"
        f"execution_id={result.execution_id}\n"
        f"jira_key={result.jira_key}"
    )


def _fail(exc: BaseException, code: int) -> NoReturn:
    """Log and print a visible Chinese error, then exit non-zero."""

    message = str(exc)
    logger.error(message)
    print(message, file=sys.stderr)
    raise SystemExit(code) from exc


def main(argv: list[str] | None = None, *, connect: ConnectFn | None = None) -> None:
    """Start a pipeline execution, or exit non-zero on a visible failure.

    Args:
        argv: Optional CLI arguments; defaults to ``sys.argv[1:]``.
        connect: Optional Temporal connector; used by tests to inject fakes.
    """

    try:
        args = parse_cli_args(argv)
        result = asyncio.run(run_start(args, connect=connect))
    except (ConfigRootError, BudgetConfigError) as exc:
        _fail(exc, 2)
    except PipelineStartError as exc:
        _fail(exc, 1)
    print(_format_success(result))


if __name__ == "__main__":
    main()
