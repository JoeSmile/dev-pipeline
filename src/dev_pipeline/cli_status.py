"""Read-only ``dev-pipeline status``: query the parent snapshot and print it.

Follow / continuous refresh lives in ``cli_status_follow``. This command
does not change Workflow state, write Jira, or write files.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from dev_pipeline.cli_ids import (
    _MISSING_CONFIG_ROOT,
    _NEED_ID,
    optional_text,
    resolve_parent_workflow_id,
)
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TEMPORAL_TARGET
from dev_pipeline.errors import ConfigRootError, PipelineStatusError
from dev_pipeline.models import PipelineSnapshot
from dev_pipeline.progress import (
    DEFAULT_DISPLAY_EXPIRY,
    DEFAULT_HEARTBEAT_INTERVAL,
    ProgressHeartbeat,
    ProgressSnapshot,
    classify_progress,
    parse_iso,
    waiting_confirmation,
)
from dev_pipeline.progress_log import load_recorded_progress
from dev_pipeline.stages import ParentStage

_CONNECT_FAILED = "无法连接 Temporal（{target}）: {exc}。查询已中止，状态未被改写。"
_QUERY_FAILED = "查询父执行失败（workflow_id={workflow_id}）: {exc}。状态未被改写。"
# 待证: follow refresh interval placeholder, aligned with the heartbeat default.
DEFAULT_FOLLOW_INTERVAL_SECONDS = DEFAULT_HEARTBEAT_INTERVAL.total_seconds()
FOLLOW_UNTIL_CHOICES = ("interrupt", "waiting", "stage-change", "expired")


class WorkflowQuerier(Protocol):
    """Minimal handle surface used to query a parent execution."""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        """Run a Workflow Query."""

    async def describe(self) -> Any:
        """Describe the execution, including pending Activity heartbeats."""


class StatusClient(Protocol):
    """Minimal Temporal client surface used by ``status``."""

    def get_workflow_handle(self, workflow_id: str) -> WorkflowQuerier:
        """Return a handle for an existing workflow id."""


class StatusConnectFn(Protocol):
    """Connect to Temporal with an explicit timeout."""

    def __call__(self, target: str, *, connect_timeout: object) -> Awaitable[StatusClient]:
        """Return a client for ``target``."""


@dataclass(frozen=True)
class StatusArgs:
    """Parsed ``dev-pipeline status`` arguments."""

    command: str
    config_root: str
    temporal_target: str
    workflow_id: str | None
    jira_key: str | None
    execution_id: str | None
    follow: bool = False
    follow_interval: float = DEFAULT_FOLLOW_INTERVAL_SECONDS
    follow_until: str = "interrupt"
    follow_max: int | None = None


def add_status_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the read-only ``status`` subcommand, including follow flags."""

    parser = sub.add_parser("status", help="只读查询父执行快照（阶段、等候、预算、心跳）。")
    parser.add_argument(
        "--config-root",
        default=None,
        help="配置根目录（绝对路径，必须已存在；用于定位进度日志，不会写入）",
    )
    parser.add_argument("--workflow-id", default=None, help="父执行 workflow id")
    parser.add_argument(
        "--jira-key",
        default=None,
        help="DP 项目的 Jira key，需同时提供 --execution-id",
    )
    parser.add_argument("--execution-id", default=None, help="本次执行 id")
    parser.add_argument(
        "--temporal-target",
        default=DEFAULT_TEMPORAL_TARGET,
        help=f"Temporal 地址（默认 {DEFAULT_TEMPORAL_TARGET}）",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="持续刷新状态，直到中断或满足 --follow-until（只读，不改状态）",
    )
    parser.add_argument(
        "--follow-interval",
        type=float,
        default=DEFAULT_FOLLOW_INTERVAL_SECONDS,
        help=(
            "跟随刷新间隔秒数（待证占位，默认 "
            f"{DEFAULT_FOLLOW_INTERVAL_SECONDS:g}，与心跳间隔占位相同）"
        ),
    )
    parser.add_argument(
        "--follow-until",
        choices=FOLLOW_UNTIL_CHOICES,
        default="interrupt",
        help="跟随结束条件：interrupt（默认，Ctrl+C 或 --follow-max）、"
        "waiting、stage-change、expired",
    )
    parser.add_argument(
        "--follow-max",
        type=int,
        default=None,
        help="最多刷新次数；省略则直到中断或 --follow-until",
    )
    return parser


def status_args_from_parse(parser: argparse.ArgumentParser, args: argparse.Namespace) -> StatusArgs:
    """Validate status flags after argparse has parsed the command line."""

    if args.config_root is None or not str(args.config_root).strip():
        parser.error(_MISSING_CONFIG_ROOT)
    workflow_id = optional_text(args.workflow_id)
    jira_key = optional_text(args.jira_key)
    execution_id = optional_text(args.execution_id)
    if workflow_id is None and (jira_key is None or execution_id is None):
        parser.error(_NEED_ID)
    interval = float(args.follow_interval)
    if interval <= 0:
        parser.error("跟随刷新间隔必须大于 0 秒。")
    follow_max = args.follow_max
    if follow_max is not None and int(follow_max) < 1:
        parser.error("--follow-max 必须是正整数。")
    return StatusArgs(
        command="status",
        config_root=str(args.config_root).strip(),
        temporal_target=str(args.temporal_target),
        workflow_id=workflow_id,
        jira_key=jira_key,
        execution_id=execution_id,
        follow=bool(args.follow),
        follow_interval=interval,
        follow_until=str(args.follow_until),
        follow_max=int(follow_max) if follow_max is not None else None,
    )


def resolve_status_workflow_id(args: StatusArgs) -> str:
    """Resolve the parent workflow id from flags. Does not start a workflow."""

    return resolve_parent_workflow_id(
        workflow_id=args.workflow_id,
        jira_key=args.jira_key,
        execution_id=args.execution_id,
    )


async def _default_connect(target: str, *, connect_timeout: object) -> StatusClient:
    from temporalio.client import Client

    return await Client.connect(target, connect_timeout=connect_timeout)  # type: ignore[arg-type]


async def query_pipeline_status(
    handle: WorkflowQuerier,
    *,
    config_root: str,
    now: datetime | None = None,
    expiry: object = DEFAULT_DISPLAY_EXPIRY,
    recorded: dict[str, object] | None = None,
) -> PipelineSnapshot:
    """Query the parent snapshot and fill in-flight progress. Read-only.

    The Temporal Query itself only returns History-backed fields plus a
    waiting-confirmation stub. Step hints and heartbeat come from the
    evidence-dir progress files and pending Activity heartbeats.
    """

    from dev_pipeline.workflows import DevPipelineWorkflow

    snap = await handle.query(DevPipelineWorkflow.snapshot)
    if not isinstance(snap, PipelineSnapshot):
        raise PipelineStatusError("查询返回了无法识别的快照。")
    clock = now or datetime.now(UTC)
    files = (
        recorded if recorded is not None else load_recorded_progress(config_root, snap.execution_id)
    )
    current_step = str(files.get("current_step") or "") or None
    last_at = str(files.get("last_heartbeat_at") or "") or None
    raw_hints = files.get("recent_hints")
    hints = [str(item) for item in raw_hints] if isinstance(raw_hints, list) else []
    pending_step, pending_at, pending_hints = await _pending_activity_progress(handle)
    if pending_step:
        current_step = pending_step
    if pending_at:
        last_at = _newer_stamp(last_at, pending_at)
    if pending_hints:
        hints = _merge_hints(hints, pending_hints)
    waiting = None
    if snap.progress is not None:
        waiting = snap.progress.waiting_confirmation
    if waiting is None:
        waiting = waiting_confirmation(snap.waiting_signal)
    parent_failed = snap.stage is ParentStage.BLOCKED
    progress = classify_progress(
        current_step=current_step,
        last_heartbeat_at=last_at,
        recent_hints=[str(item) for item in hints],
        waiting=waiting,
        now=clock,
        expiry=expiry,  # type: ignore[arg-type]
        parent_failed=parent_failed,
    )
    return replace(snap, progress=progress)


def _newer_stamp(left: str | None, right: str | None) -> str | None:
    if left is None or parse_iso(left) is None:
        return right
    if right is None or parse_iso(right) is None:
        return left
    return right if parse_iso(right) >= parse_iso(left) else left  # type: ignore[operator]


def _merge_hints(left: list[str], right: list[str]) -> list[str]:
    merged = [str(item) for item in left]
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


async def _pending_activity_progress(
    handle: WorkflowQuerier,
) -> tuple[str | None, str | None, list[str]]:
    """Read current step / last heartbeat from pending Activity details."""

    try:
        desc = await handle.describe()
    except Exception:
        return None, None, []
    raw = getattr(desc, "raw_description", None)
    pending = getattr(raw, "pending_activities", None) if raw is not None else None
    if not pending:
        return None, None, []
    step: str | None = None
    last_at: str | None = None
    hints: list[str] = []
    for info in pending:
        payload = _decode_heartbeat_details(info)
        if payload is not None:
            if payload.current_step:
                step = payload.current_step
            if payload.at:
                last_at = _newer_stamp(last_at, payload.at)
            hints.extend(payload.hints)
        stamp = getattr(info, "last_heartbeat_time", None)
        iso = _timestamp_to_iso(stamp)
        if iso:
            last_at = _newer_stamp(last_at, iso)
    return step, last_at, hints


def _decode_heartbeat_details(info: object) -> ProgressHeartbeat | None:
    details = getattr(info, "heartbeat_details", None)
    if details is None:
        details = getattr(info, "last_heartbeat_details", None)
    if not details:
        return None
    values: list[object]
    if isinstance(details, (list, tuple)):
        values = list(details)
    else:
        payloads = getattr(details, "payloads", None)
        if payloads is None:
            return None
        values = []
    if not values:
        return None
    first = values[0]
    if isinstance(first, ProgressHeartbeat):
        return first
    if isinstance(first, dict):
        return ProgressHeartbeat(
            current_step=str(first.get("current_step") or ""),
            at=str(first.get("at") or ""),
            hints=[str(item) for item in list(first.get("hints") or [])],
        )
    if isinstance(first, str):
        return ProgressHeartbeat(current_step=first, at="", hints=[first])
    return None


def _timestamp_to_iso(stamp: object) -> str | None:
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        current = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
        return current.isoformat()
    seconds = getattr(stamp, "ToDatetime", None)
    if callable(seconds):
        try:
            converted = seconds()
        except Exception:
            return None
        if isinstance(converted, datetime):
            return converted.isoformat()
    seconds_value = getattr(stamp, "seconds", None)
    nanos = getattr(stamp, "nanos", 0) or 0
    if isinstance(seconds_value, int):
        converted = datetime.fromtimestamp(seconds_value + nanos / 1_000_000_000, tz=UTC)
        return converted.isoformat()
    return None


def format_status(snap: PipelineSnapshot) -> str:
    """Render a concise, operator-visible status block."""

    progress = snap.progress or ProgressSnapshot()
    budget = snap.budget
    spent = ""
    if budget is not None:
        spent = json.dumps(budget.spent.__dict__, ensure_ascii=False, default=str)
    tickets = []
    for item in snap.tickets:
        tickets.append(f"{item.ticket_id}:{item.stage.value}")
    mirror_lagged = bool(snap.jira_mirror is not None and snap.jira_mirror.lagged)
    lines = [
        f"stage={snap.stage.value}",
        f"waiting_signal={snap.waiting_signal or ''}",
        f"waiting_confirmation={progress.waiting_confirmation or ''}",
        f"tickets={','.join(tickets)}",
        f"budget_spent={spent}",
        f"last_failure_reason={snap.last_failure_reason or ''}",
        f"last_rejection_signal={snap.last_rejection.signal if snap.last_rejection else ''}",
        f"last_rejection_reason={snap.last_rejection.reason if snap.last_rejection else ''}",
        f"jira_mirror_lagged={str(mirror_lagged).lower()}",
        f"current_step={progress.current_step or ''}",
        f"last_heartbeat_at={progress.last_heartbeat_at or ''}",
        f"heartbeat_expired={str(progress.heartbeat_expired).lower()}",
        f"unresponsive_label={progress.unresponsive_label or ''}",
        f"recent_hints={json.dumps(progress.recent_hints, ensure_ascii=False)}",
    ]
    return "\n".join(lines)


async def run_status(args: StatusArgs, *, connect: StatusConnectFn | None = None) -> str:
    """Query one parent execution and format the snapshot. No writes."""

    from dev_pipeline.config_root import validate_config_root_path

    config_root = validate_config_root_path(args.config_root)
    workflow_id = resolve_status_workflow_id(args)
    opener: StatusConnectFn = connect or _default_connect
    try:
        client = await opener(args.temporal_target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    except (PipelineStatusError, ConfigRootError):
        raise
    except Exception as exc:
        raise PipelineStatusError(
            _CONNECT_FAILED.format(target=args.temporal_target, exc=exc)
        ) from exc
    handle = client.get_workflow_handle(workflow_id)
    try:
        snap = await query_pipeline_status(handle, config_root=config_root)
    except PipelineStatusError:
        raise
    except Exception as exc:
        raise PipelineStatusError(_QUERY_FAILED.format(workflow_id=workflow_id, exc=exc)) from exc
    return format_status(snap)


__all__ = [
    "DEFAULT_FOLLOW_INTERVAL_SECONDS",
    "FOLLOW_UNTIL_CHOICES",
    "StatusArgs",
    "add_status_parser",
    "format_status",
    "query_pipeline_status",
    "resolve_status_workflow_id",
    "run_status",
    "status_args_from_parse",
]
