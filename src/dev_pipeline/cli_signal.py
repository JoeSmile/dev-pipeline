"""CLI ``dev-pipeline signal``: send an existing human Signal and show the result.

Payload validation reuses ``signal_schemas.parse_human_signal`` plus the
already-defined ``retry_ticket`` / ``drop_ticket`` ticket-id Signals.
This module does not invent a second Signal set. A rejected or wrong-stage
Signal is reported from the follow-up Query (``last_rejection``); the stage
is left unchanged by the Workflow. The CLI itself does not auto-pass a gate.
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from dev_pipeline.cli_ids import (
    _MISSING_CONFIG_ROOT,
    _NEED_ID,
    optional_text,
    resolve_parent_workflow_id,
)
from dev_pipeline.cli_signal_payload import CLI_SIGNAL_NAMES, build_signal_payload
from dev_pipeline.cli_status import query_pipeline_status
from dev_pipeline.config_root import validate_config_root_path
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TEMPORAL_TARGET
from dev_pipeline.errors import (
    ConfigRootError,
    PipelineSignalError,
    PipelineStatusError,
    PipelineUsageError,
)
from dev_pipeline.models import PipelineSnapshot, SignalRejection

_CONNECT_FAILED = "无法连接 Temporal（{target}）: {exc}。Signal 未发送，状态未被改写。"
_SIGNAL_FAILED = (
    "发送 Signal 失败（workflow_id={workflow_id}, signal={signal}）: {exc}。状态未被改写。"
)
_NOT_FOUND = "找不到父执行（workflow_id={workflow_id}）。Signal 未发送，状态未被改写。"
_QUERY_FAILED = "查询父执行失败（workflow_id={workflow_id}）: {exc}。无法确认 Signal 是否被接受。"
_NO_OBSERVED = (
    "已发送 Signal {signal}，但在超时间内未观察到状态变化或拒绝记录。阶段未被本命令改写。"
)
_UNKNOWN = "未知或不支持的 Signal: {signal}。支持: {supported}。"
SIGNAL_OBSERVE_TIMEOUT = CLIENT_CONNECT_TIMEOUT
SIGNAL_POLL_INTERVAL = timedelta(milliseconds=100)


class WorkflowSignaller(Protocol):
    """Minimal handle surface used to signal and then query a parent execution."""

    async def signal(self, *args: Any, **kwargs: Any) -> Any:
        """Send a Workflow Signal."""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        """Run a Workflow Query."""

    async def describe(self) -> Any:
        """Describe the execution."""


class SignalClient(Protocol):
    """Minimal Temporal client surface used by ``signal``."""

    def get_workflow_handle(self, workflow_id: str) -> WorkflowSignaller:
        """Return a handle for an existing workflow id."""


class SignalConnectFn(Protocol):
    """Connect to Temporal with an explicit timeout."""

    def __call__(self, target: str, *, connect_timeout: object) -> Awaitable[SignalClient]:
        """Return a client for ``target``."""


@dataclass(frozen=True)
class SignalArgs:
    """Parsed ``dev-pipeline signal`` arguments."""

    command: str
    name: str
    config_root: str
    temporal_target: str
    workflow_id: str | None
    jira_key: str | None
    execution_id: str | None
    payload: object


def add_signal_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``signal`` subcommand."""

    parser = sub.add_parser(
        "signal",
        help="向父执行发送已定义的人工 Signal，并用 Query 确认结果。",
    )
    parser.add_argument(
        "signal_name",
        nargs="?",
        default=None,
        help="Signal 名称，例如 lock_scope；也可改用 --name",
    )
    parser.add_argument("--name", default=None, help="与位置参数等价的 Signal 名称")
    parser.add_argument(
        "--config-root",
        default=None,
        help="配置根目录（绝对路径，必须已存在；查询进度时只读，不会写入）",
    )
    parser.add_argument("--workflow-id", default=None, help="父执行 workflow id")
    parser.add_argument(
        "--jira-key",
        default=None,
        help="DP 项目的 Jira key，需同时提供 --execution-id",
    )
    parser.add_argument("--execution-id", default=None, help="本次执行 id（用于定位父执行）")
    parser.add_argument(
        "--temporal-target",
        default=DEFAULT_TEMPORAL_TARGET,
        help=f"Temporal 地址（默认 {DEFAULT_TEMPORAL_TARGET}）",
    )
    parser.add_argument("--payload", default=None, help="JSON 对象，作为 Signal payload")
    parser.add_argument("--payload-file", default=None, help="从 UTF-8 JSON 文件读取 payload")
    parser.add_argument("--in-scope", dest="in_scope", default=None, help="lock_scope.in_scope")
    parser.add_argument(
        "--out-of-scope", dest="out_of_scope", default=None, help="lock_scope.out_of_scope"
    )
    parser.add_argument("--locked-by", dest="locked_by", default=None, help="lock_scope.locked_by")
    parser.add_argument("--tradeoff", default=None, help="decide_scheme.tradeoff")
    parser.add_argument("--hypotheses", default=None, help="decide_scheme.hypotheses（JSON 数组）")
    parser.add_argument(
        "--decided-by",
        dest="decided_by",
        default=None,
        help="decide_scheme / review_override.decided_by",
    )
    parser.add_argument("--notes", default=None, help="decide_scheme.notes")
    parser.add_argument(
        "--tickets",
        default=None,
        help="decide_scheme / submit_tickets.tickets（JSON 数组）",
    )
    parser.add_argument(
        "--accepted", default=None, help="approve_spike.accepted（true/false 或 是/否）"
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="abort.reason / promote_rule.reason / review_override.reason",
    )
    parser.add_argument(
        "--ticket-id",
        dest="ticket_id",
        default=None,
        help="review_override / retry_ticket / drop_ticket",
    )
    parser.add_argument(
        "--decision", default=None, help="review_override.decision（放行/打回/否决）"
    )
    parser.add_argument(
        "--candidate-id", dest="candidate_id", default=None, help="promote_rule.candidate_id"
    )
    parser.add_argument("--target", default=None, help="promote_rule.target（L0 或 全局 Skill）")
    parser.add_argument(
        "--evidence-citation",
        dest="evidence_citation",
        default=None,
        help="promote_rule.evidence_citation",
    )
    parser.add_argument(
        "--automatic",
        default=None,
        help="promote_rule.automatic（true/false，人工晋升应省略或 false）",
    )
    parser.add_argument(
        "--new-execution-id",
        dest="new_execution_id",
        default=None,
        help="reopen 的新 execution_id，不得与当前执行相同",
    )
    parser.add_argument(
        "--submitted-by", dest="submitted_by", default=None, help="submit_tickets.submitted_by"
    )
    return parser


def signal_args_from_parse(parser: argparse.ArgumentParser, args: argparse.Namespace) -> SignalArgs:
    """Validate signal flags after argparse has parsed the command line."""

    if args.config_root is None or not str(args.config_root).strip():
        parser.error(_MISSING_CONFIG_ROOT)
    positional = optional_text(args.signal_name)
    named = optional_text(args.name)
    if positional and named and positional != named:
        parser.error("位置参数与 --name 不一致，请只提供一个 Signal 名称。")
    name = positional or named
    if name is None:
        parser.error(
            "请传入 Signal 名称，例如: dev-pipeline signal lock_scope ... 或 --name lock_scope"
        )
    if name not in CLI_SIGNAL_NAMES:
        parser.error(_UNKNOWN.format(signal=name, supported=", ".join(CLI_SIGNAL_NAMES)))
    workflow_id = optional_text(args.workflow_id)
    jira_key = optional_text(args.jira_key)
    execution_id = optional_text(args.execution_id)
    if workflow_id is None and (jira_key is None or execution_id is None):
        parser.error(_NEED_ID)
    try:
        payload = build_signal_payload(name, args)
    except PipelineUsageError as exc:
        parser.error(str(exc))
    return SignalArgs(
        command="signal",
        name=name,
        config_root=str(args.config_root).strip(),
        temporal_target=str(args.temporal_target),
        workflow_id=workflow_id,
        jira_key=jira_key,
        execution_id=execution_id,
        payload=payload,
    )


def format_signal_success(name: str, snap: PipelineSnapshot) -> str:
    """Return operator-visible text after a Signal was accepted."""

    progress = snap.progress
    waiting = progress.waiting_confirmation if progress is not None else None
    return "\n".join(
        [
            f"已接受 Signal {name}。",
            f"stage={snap.stage.value}",
            f"waiting_signal={snap.waiting_signal or ''}",
            f"waiting_confirmation={waiting or ''}",
        ]
    )


def format_signal_rejection(name: str, snap: PipelineSnapshot) -> str:
    """Return operator-visible text when the Workflow rejected the Signal."""

    reason = snap.last_rejection.reason if snap.last_rejection is not None else "未知原因"
    return "\n".join(
        [
            "Signal 已被拒绝，阶段未改变。",
            f"signal={name}",
            f"reason={reason}",
            f"stage={snap.stage.value}",
            f"waiting_signal={snap.waiting_signal or ''}",
        ]
    )


def _observable(snap: PipelineSnapshot) -> tuple[object, ...]:
    tickets = tuple(
        (item.ticket_id, item.stage.value, item.detail, item.unhandled_failure)
        for item in snap.tickets
    )
    scope = None
    if snap.scope is not None:
        scope = (snap.scope.in_scope, snap.scope.out_of_scope, snap.scope.locked_by)
    scheme = snap.scheme.tradeoff if snap.scheme is not None else None
    return (
        snap.stage.value,
        snap.waiting_signal,
        tickets,
        snap.promote_rule_status,
        snap.test_deploy_status.value,
        snap.reopen_status,
        snap.reopened_execution_id,
        snap.ticket_list_complete,
        snap.ticket_list_submitted_by,
        snap.blocked_reason,
        scope,
        scheme,
        snap.promotion is not None,
    )


def _rejection_key(item: SignalRejection | None) -> tuple[str, str] | None:
    if item is None:
        return None
    return (item.signal, item.reason)


def _is_not_found(exc: BaseException) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    if status is not None and "NOT_FOUND" in str(status):
        return True
    text = str(exc).lower()
    return "not found" in text or "no row" in text or "notfound" in text


async def _settle_snapshot(
    handle: WorkflowSignaller,
    *,
    config_root: str,
    before: PipelineSnapshot,
    current: PipelineSnapshot,
    sleeper: Callable[[float], Awaitable[None]],
    clock: Callable[[], datetime],
    poll_interval: timedelta,
) -> PipelineSnapshot:
    """Poll briefly so waiting_signal can catch up after a stage change."""

    if current.stage is before.stage or current.waiting_signal != before.waiting_signal:
        return current
    settled = current
    deadline = clock() + timedelta(seconds=2)
    while clock() < deadline:
        await sleeper(poll_interval.total_seconds())
        try:
            nxt = await query_pipeline_status(handle, config_root=config_root)
        except Exception:
            return settled
        settled = nxt
        # Stop early once waiting_signal no longer lags behind the new stage.
        if settled.waiting_signal != current.waiting_signal or settled.stage is not current.stage:
            return settled
    return settled


async def run_signal(
    args: SignalArgs,
    *,
    connect: SignalConnectFn | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    observe_timeout: timedelta = SIGNAL_OBSERVE_TIMEOUT,
    poll_interval: timedelta = SIGNAL_POLL_INTERVAL,
    now: Callable[[], datetime] | None = None,
) -> str:
    """Send one human Signal and confirm acceptance or rejection via Query."""

    import asyncio

    config_root = validate_config_root_path(args.config_root)
    workflow_id = resolve_parent_workflow_id(
        workflow_id=args.workflow_id,
        jira_key=args.jira_key,
        execution_id=args.execution_id,
    )
    opener = connect or _default_connect
    try:
        client = await opener(args.temporal_target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    except (PipelineSignalError, PipelineStatusError, ConfigRootError):
        raise
    except Exception as exc:
        raise PipelineSignalError(
            _CONNECT_FAILED.format(target=args.temporal_target, exc=exc)
        ) from exc
    handle = client.get_workflow_handle(workflow_id)
    try:
        before = await query_pipeline_status(handle, config_root=config_root)
    except PipelineStatusError as exc:
        if _is_not_found(exc):
            raise PipelineSignalError(_NOT_FOUND.format(workflow_id=workflow_id)) from exc
        raise PipelineSignalError(_QUERY_FAILED.format(workflow_id=workflow_id, exc=exc)) from exc
    except Exception as exc:
        if _is_not_found(exc):
            raise PipelineSignalError(_NOT_FOUND.format(workflow_id=workflow_id)) from exc
        raise PipelineSignalError(_QUERY_FAILED.format(workflow_id=workflow_id, exc=exc)) from exc
    try:
        if args.payload is None:
            await handle.signal(args.name)
        else:
            await handle.signal(args.name, args.payload)
    except Exception as exc:
        if _is_not_found(exc):
            raise PipelineSignalError(_NOT_FOUND.format(workflow_id=workflow_id)) from exc
        raise PipelineSignalError(
            _SIGNAL_FAILED.format(workflow_id=workflow_id, signal=args.name, exc=exc)
        ) from exc
    sleeper = sleep or asyncio.sleep
    clock = now or (lambda: datetime.now(UTC))
    deadline = clock() + observe_timeout
    before_rejection = _rejection_key(before.last_rejection)
    before_key = _observable(before)
    after = before
    while True:
        try:
            after = await query_pipeline_status(handle, config_root=config_root)
        except Exception as exc:
            raise PipelineSignalError(
                _QUERY_FAILED.format(workflow_id=workflow_id, exc=exc)
            ) from exc
        rejection = _rejection_key(after.last_rejection)
        if (
            rejection is not None
            and rejection != before_rejection
            and after.last_rejection is not None
        ):
            if after.last_rejection.signal == args.name:
                if after.stage is before.stage:
                    raise PipelineSignalError(format_signal_rejection(args.name, after))
        if _observable(after) != before_key:
            return format_signal_success(
                args.name,
                await _settle_snapshot(
                    handle,
                    config_root=config_root,
                    before=before,
                    current=after,
                    sleeper=sleeper,
                    clock=clock,
                    poll_interval=poll_interval,
                ),
            )
        if clock() >= deadline:
            if (
                after.last_rejection is not None
                and after.last_rejection.signal == args.name
                and after.stage is before.stage
            ):
                raise PipelineSignalError(format_signal_rejection(args.name, after))
            raise PipelineSignalError(_NO_OBSERVED.format(signal=args.name))
        await sleeper(poll_interval.total_seconds())


async def _default_connect(target: str, *, connect_timeout: object) -> SignalClient:
    from temporalio.client import Client

    return await Client.connect(target, connect_timeout=connect_timeout)  # type: ignore[arg-type]


__all__ = [
    "CLI_SIGNAL_NAMES",
    "SignalArgs",
    "add_signal_parser",
    "build_signal_payload",
    "format_signal_rejection",
    "format_signal_success",
    "run_signal",
    "signal_args_from_parse",
]
