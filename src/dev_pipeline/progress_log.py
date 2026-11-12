"""UTF-8 progress log and heartbeat file under this execution's evidence dir.

Workflow modules must not import this file. The log is the Activity-side
source for step hints; Temporal Activity heartbeats carry the current step.
The full log is never written into Workflow History.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dev_pipeline.progress import (
    DEFAULT_HEARTBEAT_INTERVAL,
    MAX_RECENT_HINTS,
    ProgressHeartbeat,
    sanitize_step,
)
from dev_pipeline.workspace_config import resolve_knowledge_root

PROGRESS_RELDIR = "docs/knowledge/executions"
PROGRESS_LOG_NAME = "progress.log"
HEARTBEAT_FILE_NAME = "heartbeat.json"
_SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


def _safe_execution_id(execution_id: str) -> str | None:
    text = execution_id.strip()
    if not text or not _SAFE.fullmatch(text) or text in {".", ".."}:
        return None
    return text


def execution_evidence_dir(knowledge_root: str, execution_id: str) -> Path | None:
    """Return this execution's evidence directory, or None when unsafe/missing."""

    root = knowledge_root.strip()
    safe = _safe_execution_id(execution_id)
    if not root or not safe:
        return None
    return Path(root) / PROGRESS_RELDIR / safe


def progress_log_path(knowledge_root: str, execution_id: str) -> Path | None:
    """Return the UTF-8 progress log path for this execution."""

    directory = execution_evidence_dir(knowledge_root, execution_id)
    if directory is None:
        return None
    return directory / PROGRESS_LOG_NAME


def heartbeat_path(knowledge_root: str, execution_id: str) -> Path | None:
    """Return the last-heartbeat file path for this execution."""

    directory = execution_evidence_dir(knowledge_root, execution_id)
    if directory is None:
        return None
    return directory / HEARTBEAT_FILE_NAME


def _iso(now: datetime) -> str:
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return stamp.isoformat()


def append_progress_step(
    log_path: Path,
    step: str,
    *,
    now: datetime | None = None,
) -> str:
    """Append one sanitized step hint as a UTF-8 JSONL line. Returns the step."""

    cleaned = sanitize_step(step)
    if not cleaned:
        return ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _iso(now or datetime.now(UTC)), "kind": "step", "step": cleaned}
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cleaned


def write_heartbeat_file(
    path: Path,
    step: str,
    *,
    now: datetime | None = None,
    ticket_id: str = "",
    activity_name: str = "",
) -> str:
    """Overwrite the last-heartbeat file. Quiet heartbeats do not append hints."""

    cleaned = sanitize_step(step)
    stamp = _iso(now or datetime.now(UTC))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current_step": cleaned,
        "last_heartbeat_at": stamp,
        "ticket_id": ticket_id,
        "activity": activity_name,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return stamp


def read_recent_hints(log_path: Path | None, *, limit: int = MAX_RECENT_HINTS) -> list[str]:
    """Return the most recent sanitized step hints. Missing files yield []."""

    if log_path is None or not log_path.is_file():
        return []
    hints: list[str] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(data.get("kind") or "step") != "step":
            continue
        step = sanitize_step(str(data.get("step") or ""))
        if step:
            hints.append(step)
    return hints[-limit:]


def read_heartbeat_file(path: Path | None) -> dict[str, str]:
    """Return the last heartbeat record, or an empty dict when missing."""

    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value is not None}


def resolve_execution_knowledge_root(config_root: str) -> str:
    """Resolve knowledge_root from config-root, or empty when unset/invalid."""

    root = config_root.strip()
    if not root:
        return ""
    try:
        return resolve_knowledge_root(root)
    except Exception:
        return ""


def _emit_temporal_heartbeat(payload: ProgressHeartbeat) -> None:
    try:
        from temporalio import activity
    except Exception:
        return
    if activity.in_activity():
        activity.heartbeat(payload)


class ProgressSession:
    """Write step hints, keep a last-heartbeat file, and Temporal-heartbeat."""

    def __init__(
        self,
        *,
        knowledge_root: str,
        execution_id: str,
        ticket_id: str = "",
        activity_name: str = "",
        interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
        initial_step: str = "",
        quiet_heartbeat: bool = True,
    ) -> None:
        self._knowledge_root = knowledge_root
        self._execution_id = execution_id
        self._ticket_id = ticket_id
        self._activity_name = activity_name
        self._interval = interval
        self._quiet_heartbeat = quiet_heartbeat
        self._step = sanitize_step(initial_step) or "正在执行"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._log_path = progress_log_path(knowledge_root, execution_id)
        self._heartbeat_file = heartbeat_path(knowledge_root, execution_id)

    def update(self, step: str) -> None:
        """Record a new step hint and heartbeat immediately."""

        cleaned = sanitize_step(step)
        if not cleaned:
            return
        self._step = cleaned
        now = datetime.now(UTC)
        if self._log_path is not None:
            append_progress_step(self._log_path, cleaned, now=now)
        self._heartbeat(now)

    def _heartbeat(self, now: datetime | None = None) -> None:
        stamp = now or datetime.now(UTC)
        if self._heartbeat_file is not None:
            iso = write_heartbeat_file(
                self._heartbeat_file,
                self._step,
                now=stamp,
                ticket_id=self._ticket_id,
                activity_name=self._activity_name,
            )
        else:
            iso = _iso(stamp)
        _emit_temporal_heartbeat(
            ProgressHeartbeat(current_step=self._step, at=iso, hints=[self._step])
        )

    async def _loop(self) -> None:
        timeout = self._interval.total_seconds()
        if timeout <= 0:
            timeout = DEFAULT_HEARTBEAT_INTERVAL.total_seconds()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except TimeoutError:
                self._heartbeat()

    async def start(self) -> None:
        """Start quiet heartbeats. The first step is recorded when given."""

        if self._step:
            now = datetime.now(UTC)
            if self._log_path is not None and self._step != "正在执行":
                append_progress_step(self._log_path, self._step, now=now)
            self._heartbeat(now)
        if self._quiet_heartbeat:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the quiet-heartbeat loop."""

        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task


@asynccontextmanager
async def activity_progress(
    *,
    config_root: str,
    execution_id: str,
    ticket_id: str = "",
    activity_name: str = "",
    initial_step: str = "",
    interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
    quiet_heartbeat: bool = True,
) -> AsyncIterator[ProgressSession]:
    """Context manager for long-running Activities to emit hints + heartbeats."""

    knowledge = resolve_execution_knowledge_root(config_root)
    session = ProgressSession(
        knowledge_root=knowledge,
        execution_id=execution_id,
        ticket_id=ticket_id,
        activity_name=activity_name,
        interval=interval,
        initial_step=initial_step,
        quiet_heartbeat=quiet_heartbeat,
    )
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


def load_recorded_progress(config_root: str, execution_id: str) -> dict[str, Any]:
    """Read the progress log and heartbeat file without writing anything."""

    knowledge = resolve_execution_knowledge_root(config_root)
    log = progress_log_path(knowledge, execution_id)
    beat = heartbeat_path(knowledge, execution_id)
    record = read_heartbeat_file(beat)
    return {
        "current_step": record.get("current_step") or "",
        "last_heartbeat_at": record.get("last_heartbeat_at") or "",
        "recent_hints": read_recent_hints(log),
    }


__all__ = [
    "HEARTBEAT_FILE_NAME",
    "PROGRESS_LOG_NAME",
    "PROGRESS_RELDIR",
    "ProgressSession",
    "activity_progress",
    "append_progress_step",
    "execution_evidence_dir",
    "heartbeat_path",
    "load_recorded_progress",
    "progress_log_path",
    "read_heartbeat_file",
    "read_recent_hints",
    "resolve_execution_knowledge_root",
    "write_heartbeat_file",
]
