"""Queryable in-flight progress types and History-safe classifiers (DP-27).

This module is deterministic and does no filesystem, clock, network, or CLI
I/O so Workflow code may import it. Activity heartbeats and the UTF-8
progress log live in ``progress_log``. The heartbeat interval and display
expiry below are 待证 placeholders, not a signed SLA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 待证: not a signed SLA. Activities keep heartbeating at this interval
# even when the current step has not changed.
DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=2)

# 待证: display-only expiry, independent of Temporal heartbeat_timeout.
# When the last heartbeat is older than this, the snapshot is labeled
# 「可能无响应」 without failing the Workflow by itself.
DEFAULT_DISPLAY_EXPIRY = timedelta(seconds=10)

# 待证: Query returns only a limited number of recent step hints.
MAX_RECENT_HINTS = 8

# Short hint length; longer text is truncated and never treated as thinking.
MAX_STEP_CHARS = 200

UNRESPONSIVE_LABEL = "可能无响应"
WAITING_PREFIX = "正在等候你确认"

# waiting_signal values that are already a full 「正在等候你确认…」 phrase.
_WAITING_PHRASES = frozenset({"正在等候你确认测试环境发布"})

_SIGNAL_CONFIRM_LABELS = {
    "lock_scope": "锁定范围",
    "decide_scheme": "方案取舍",
    "submit_tickets": "提交票单",
    "approve_spike": "Spike 结论",
    "review_override": "审查裁决",
    "authorize_test_deploy": "测试环境发布",
    "promote_rule": "规则晋升",
    "retry_ticket_or_drop_ticket_or_abort": "重试、移除该票或中止",
}

_SECRET_MARKERS = (
    "CURSOR_API_KEY",
    "api_key",
    "api-key",
    "apikey",
    "secret",
    "password",
    "token=",
)


@dataclass
class ProgressHeartbeat:
    """Small Activity heartbeat payload. Never carries secrets or thinking."""

    current_step: str
    at: str
    hints: list[str] = field(default_factory=list)


@dataclass
class ProgressSnapshot:
    """In-flight view hung off the existing parent Query snapshot."""

    current_step: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_expired: bool = False
    unresponsive_label: str | None = None
    recent_hints: list[str] = field(default_factory=list)
    waiting_confirmation: str | None = None


def sanitize_step(step: str) -> str:
    """Return a short, secret-free hint. Full thinking is dropped."""

    text = " ".join(step.split())
    if not text:
        return ""
    lowered = text.lower()
    if any(marker.lower() in lowered or marker in text for marker in _SECRET_MARKERS):
        return "（已隐藏敏感内容）"
    if len(text) > MAX_STEP_CHARS:
        return text[: MAX_STEP_CHARS - 1] + "…"
    return text


def waiting_confirmation(
    waiting_signal: str | None,
    human_gate_ticket_ids: tuple[str, ...] = (),
) -> str | None:
    """Return 「正在等候你确认××」, or None when nothing is being waited on."""

    if waiting_signal:
        text = waiting_signal.strip()
        if text in _WAITING_PHRASES or text.startswith(WAITING_PREFIX):
            return text
        label = _SIGNAL_CONFIRM_LABELS.get(text, text)
        return f"{WAITING_PREFIX}{label}"
    if human_gate_ticket_ids:
        ticket = human_gate_ticket_ids[0]
        return f"{WAITING_PREFIX}审查裁决（{ticket}）"
    return None


def last_failure_reason(
    *,
    blocked_reason: str | None,
    ticket_failures: tuple[tuple[str, str, str], ...] = (),
    budget_stop: str | None = None,
    budget_breach_reason: str | None = None,
) -> str | None:
    """Pick a History-backed last failure reason for the Query snapshot."""

    if blocked_reason and blocked_reason.strip():
        return blocked_reason.strip()
    if ticket_failures:
        ticket_id, stage, detail = ticket_failures[-1]
        if detail.strip():
            return detail.strip()
        return f"{ticket_id} 处于 {stage}"
    if budget_stop and budget_stop.strip():
        return budget_stop.strip()
    if budget_breach_reason and budget_breach_reason.strip():
        return budget_breach_reason.strip()
    return None


def is_heartbeat_expired(
    last_heartbeat_at: datetime | None,
    now: datetime,
    expiry: timedelta = DEFAULT_DISPLAY_EXPIRY,
) -> bool:
    """Return True when the last heartbeat is older than the display expiry."""

    if last_heartbeat_at is None:
        return False
    last = last_heartbeat_at
    current = now
    if last.tzinfo is None:
        last = last.replace(tzinfo=current.tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=last.tzinfo)
    return current - last > expiry


def classify_progress(
    *,
    current_step: str | None,
    last_heartbeat_at: str | None,
    recent_hints: list[str],
    waiting: str | None,
    now: datetime,
    expiry: timedelta = DEFAULT_DISPLAY_EXPIRY,
    parent_failed: bool = False,
) -> ProgressSnapshot:
    """Build the in-flight snapshot. Waiting is never labeled unresponsive.

    An expired display does not itself mark the Workflow Failed. A parent
    already in Failed / Blocked also does not use the unresponsive label.
    """

    waiting_text = waiting
    parsed = parse_iso(last_heartbeat_at)
    expired = is_heartbeat_expired(parsed, now, expiry)
    show_unresponsive = expired and not waiting_text and not parent_failed
    hints = [sanitize_step(item) for item in recent_hints if sanitize_step(item)]
    limited = hints[-MAX_RECENT_HINTS:]
    step = waiting_text or (sanitize_step(current_step) if current_step else None)
    return ProgressSnapshot(
        current_step=step or None,
        last_heartbeat_at=last_heartbeat_at,
        heartbeat_expired=expired,
        unresponsive_label=UNRESPONSIVE_LABEL if show_unresponsive else None,
        recent_hints=limited,
        waiting_confirmation=waiting_text,
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, accepting a trailing Z."""

    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def hint_from_stream_json(event: dict[str, object]) -> str | None:
    """Reserved Cursor ``stream-json`` converter. Does not start a CLI.

    Thinking and secrets are dropped. This is a stub for DP-46 / DP-47
    runtime wiring and is not connected to a live Cursor process.
    """

    kind = str(event.get("type") or event.get("event") or "").strip().lower()
    if kind in {"thinking", "reasoning", "system"}:
        return None
    raw = event.get("hint") or event.get("title") or event.get("step") or event.get("name")
    if raw is None:
        return None
    return sanitize_step(str(raw)) or None


__all__ = [
    "DEFAULT_DISPLAY_EXPIRY",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "MAX_RECENT_HINTS",
    "MAX_STEP_CHARS",
    "UNRESPONSIVE_LABEL",
    "WAITING_PREFIX",
    "ProgressHeartbeat",
    "ProgressSnapshot",
    "classify_progress",
    "hint_from_stream_json",
    "is_heartbeat_expired",
    "last_failure_reason",
    "parse_iso",
    "sanitize_step",
    "waiting_confirmation",
]
