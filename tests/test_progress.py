"""Unit tests for DP-27 progress classification, log I/O, and redaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from dev_pipeline.progress import (
    DEFAULT_DISPLAY_EXPIRY,
    UNRESPONSIVE_LABEL,
    classify_progress,
    hint_from_stream_json,
    last_failure_reason,
    sanitize_step,
    waiting_confirmation,
)
from dev_pipeline.progress_log import (
    HEARTBEAT_FILE_NAME,
    PROGRESS_LOG_NAME,
    append_progress_step,
    load_recorded_progress,
    progress_log_path,
    read_recent_hints,
    write_heartbeat_file,
)
from dev_pipeline.workspace_config import resolve_knowledge_root


def test_waiting_confirmation_uses_chinese_phrase() -> None:
    assert waiting_confirmation("lock_scope") == "正在等候你确认锁定范围"
    assert waiting_confirmation("decide_scheme") == "正在等候你确认方案取舍"
    assert waiting_confirmation("正在等候你确认测试环境发布") == "正在等候你确认测试环境发布"
    assert waiting_confirmation(None, ("T-9",)) == "正在等候你确认审查裁决（T-9）"
    assert waiting_confirmation(None) is None


def test_expired_heartbeat_is_labeled_unresponsive_not_failed() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    stale = (now - DEFAULT_DISPLAY_EXPIRY - timedelta(seconds=1)).isoformat()
    snap = classify_progress(
        current_step="正在写代码：实现登录校验",
        last_heartbeat_at=stale,
        recent_hints=["正在写代码：实现登录校验"],
        waiting=None,
        now=now,
    )
    assert snap.heartbeat_expired is True
    assert snap.unresponsive_label == UNRESPONSIVE_LABEL
    assert snap.waiting_confirmation is None
    assert snap.current_step == "正在写代码：实现登录校验"


def test_waiting_is_not_labeled_unresponsive_even_if_heartbeat_is_stale() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    stale = (now - timedelta(hours=1)).isoformat()
    waiting = waiting_confirmation("lock_scope")
    snap = classify_progress(
        current_step="ignored",
        last_heartbeat_at=stale,
        recent_hints=[],
        waiting=waiting,
        now=now,
    )
    assert snap.waiting_confirmation == "正在等候你确认锁定范围"
    assert snap.current_step == "正在等候你确认锁定范围"
    assert snap.unresponsive_label is None
    assert UNRESPONSIVE_LABEL not in (snap.current_step or "")


def test_blocked_parent_is_not_labeled_unresponsive() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    stale = (now - timedelta(hours=1)).isoformat()
    snap = classify_progress(
        current_step="正在写代码：实现登录校验",
        last_heartbeat_at=stale,
        recent_hints=[],
        waiting=None,
        now=now,
        parent_failed=True,
    )
    assert snap.unresponsive_label is None
    assert snap.heartbeat_expired is True


def test_last_failure_reason_prefers_blocked_then_ticket_detail() -> None:
    assert last_failure_reason(blocked_reason="abort") == "abort"
    assert (
        last_failure_reason(
            blocked_reason=None,
            ticket_failures=(("T1", "Failed", "implement failed: T1"),),
        )
        == "implement failed: T1"
    )
    assert (
        last_failure_reason(
            blocked_reason=None,
            budget_stop="budget-stop",
        )
        == "budget-stop"
    )


def test_sanitize_and_stream_json_drop_secrets_and_thinking() -> None:
    assert sanitize_step("CURSOR_API_KEY=abcd") == "（已隐藏敏感内容）"
    assert hint_from_stream_json({"type": "thinking", "step": "长篇推理"}) is None
    assert hint_from_stream_json({"type": "tool", "title": "正在读取仓库"}) == "正在读取仓库"


def test_progress_log_appends_utf8_steps_and_limits_hints(tmp_path: Path) -> None:
    log = tmp_path / PROGRESS_LOG_NAME
    append_progress_step(log, "正在读取仓库", now=datetime(2026, 11, 12, tzinfo=UTC))
    append_progress_step(log, "正在跑单测", now=datetime(2026, 11, 12, 0, 1, tzinfo=UTC))
    text = log.read_text(encoding="utf-8")
    assert "正在读取仓库" in text
    assert "正在跑单测" in text
    assert "CURSOR_API_KEY" not in text
    hints = read_recent_hints(log)
    assert hints == ["正在读取仓库", "正在跑单测"]


def test_query_side_helpers_do_not_write_files(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    knowledge = resolve_knowledge_root(str(config))
    path = progress_log_path(knowledge, "exec-1")
    assert path is not None
    before = list(config.rglob("*"))
    recorded = load_recorded_progress(str(config), "exec-1")
    assert recorded["recent_hints"] == []
    assert recorded["current_step"] == ""
    after = list(config.rglob("*"))
    assert after == before
    assert not (tmp_path / "config" / "docs").exists()


def test_heartbeat_file_updates_without_appending_hints(tmp_path: Path) -> None:
    beat = tmp_path / HEARTBEAT_FILE_NAME
    stamp = write_heartbeat_file(beat, "正在写代码：实现登录校验")
    assert "正在写代码：实现登录校验" in beat.read_text(encoding="utf-8")
    write_heartbeat_file(beat, "正在写代码：实现登录校验")
    assert beat.read_text(encoding="utf-8").count("正在写代码") == 1
    assert stamp
