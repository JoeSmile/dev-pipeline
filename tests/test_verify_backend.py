"""Filesystem tests for the deterministic verifying backend and evidence writes."""

from __future__ import annotations

from pathlib import Path

from tests.sandbox_fixtures import write_workspace_toml

from dev_pipeline.review import MAX_FILE_LINES
from dev_pipeline.verify import (
    CITATION_LINE_LIMIT_VERIFY,
    CITATION_REPRODUCIBLE_EVIDENCE,
    DEFAULT_EVIDENCE_RELPATH,
    FIELD_COMMAND,
    FIELD_EVIDENCE_PATH,
    FIELD_RED_LINE,
    FIELD_RESULT,
    RESULT_BROKEN,
    RESULT_DRY_RUN,
    RESULT_HELD,
    RESULT_HISTORY_ONLY,
    VERIFY_MODE_DRY_RUN,
    VERIFY_MODE_HISTORY,
    VerifyWorkInput,
    parse_evidence_text,
)
from dev_pipeline.verify_backend import produce_verify_result
from dev_pipeline.workspace import reset_default_claim_store


def _payload(tmp_config_root: Path, authorized: Path, **kwargs: object) -> VerifyWorkInput:
    write_workspace_toml(tmp_config_root, authorized)
    worktree = authorized / "T1" / "exec-1"
    worktree.mkdir(parents=True, exist_ok=True)
    fields: dict[str, object] = {
        "ticket_id": "T1",
        "execution_id": "exec-1",
        "story_id": "STORY-1",
        "config_root": str(tmp_config_root),
        "authorized_worktree": str(worktree),
    }
    fields.update(kwargs)
    return VerifyWorkInput(**fields)  # type: ignore[arg-type]


def test_passing_check_writes_utf8_evidence_mapped_to_red_line(
    tmp_config_root: Path,
) -> None:
    reset_default_claim_store()
    try:
        authorized = tmp_config_root.parent / "authorized-root"
        payload = _payload(tmp_config_root, authorized)
        (Path(payload.authorized_worktree) / "ok.py").write_text(
            "print('你好')\n", encoding="utf-8"
        )
        result = produce_verify_result(payload)
        assert result.passed is True
        assert result.record is not None
        assert result.record.passed is True
        assert any(item.result == RESULT_HELD for item in result.record.lines)
        assert any(CITATION_REPRODUCIBLE_EVIDENCE in item.red_line for item in result.record.lines)
        evidence = Path(result.record.evidence_path)
        assert evidence == Path(payload.authorized_worktree) / DEFAULT_EVIDENCE_RELPATH
        raw = evidence.read_bytes()
        text = raw.decode("utf-8")
        assert FIELD_RED_LINE.encode("utf-8") in raw
        assert FIELD_COMMAND.encode("utf-8") in raw
        assert FIELD_RESULT.encode("utf-8") in raw
        assert FIELD_EVIDENCE_PATH.encode("utf-8") in raw
        parsed = parse_evidence_text(text)
        assert parsed.ticket_id == "T1"
        assert parsed.execution_id == "exec-1"
        assert parsed.passed is True
        assert parsed.lines
    finally:
        reset_default_claim_store()


def test_oversized_file_fails_and_still_writes_evidence(tmp_config_root: Path) -> None:
    reset_default_claim_store()
    try:
        authorized = tmp_config_root.parent / "authorized-huge"
        payload = _payload(tmp_config_root, authorized)
        huge = Path(payload.authorized_worktree) / "huge.py"
        huge.write_text("\n".join(f"line-{i}" for i in range(MAX_FILE_LINES + 1)), encoding="utf-8")
        result = produce_verify_result(payload)
        assert result.passed is False
        assert result.record is not None
        broken = [
            item for item in result.record.lines if item.red_line == CITATION_LINE_LIMIT_VERIFY
        ]
        assert broken and broken[0].result == RESULT_BROKEN
        evidence = Path(result.record.evidence_path)
        assert evidence.is_file()
        text = evidence.read_text(encoding="utf-8")
        assert RESULT_BROKEN in text
        assert evidence.read_bytes().decode("utf-8") == text
    finally:
        reset_default_claim_store()


def test_dry_run_and_history_only_write_evidence_but_do_not_pass(
    tmp_config_root: Path,
) -> None:
    reset_default_claim_store()
    try:
        authorized = tmp_config_root.parent / "authorized-probe"
        dry = produce_verify_result(_payload(tmp_config_root, authorized, mode=VERIFY_MODE_DRY_RUN))
        assert dry.passed is False
        assert dry.record is not None
        assert dry.record.lines[0].result == RESULT_DRY_RUN
        assert Path(dry.record.evidence_path).is_file()
        history = produce_verify_result(
            _payload(tmp_config_root, authorized, mode=VERIFY_MODE_HISTORY)
        )
        assert history.passed is False
        assert history.record is not None
        assert history.record.lines[0].result == RESULT_HISTORY_ONLY
        assert "dry-run 或仅 History" in (history.record.reason or history.evidence)
    finally:
        reset_default_claim_store()


def test_out_of_bounds_evidence_write_is_rejected(tmp_config_root: Path) -> None:
    reset_default_claim_store()
    try:
        authorized = tmp_config_root.parent / "authorized-oob"
        outside = tmp_config_root.parent / "external" / "evidence.md"
        outside.parent.mkdir()
        payload = _payload(
            tmp_config_root,
            authorized,
            evidence_path=str(outside),
        )
        result = produce_verify_result(payload)
        assert result.passed is False
        assert result.record is not None
        assert result.record.write_rejected is True
        assert "越出" in result.record.write_reason or "拒绝" in result.record.write_reason
        assert not outside.exists()
    finally:
        reset_default_claim_store()


def test_scheme_red_line_broken_status_fails(tmp_config_root: Path) -> None:
    reset_default_claim_store()
    try:
        authorized = tmp_config_root.parent / "authorized-hyp"
        payload = _payload(
            tmp_config_root,
            authorized,
            scheme_red_lines=["该假设可能击穿 L0"],
        )
        status = Path(payload.authorized_worktree) / "RED_LINE_STATUS.md"
        status.write_text("未守住: 该假设可能击穿 L0\n", encoding="utf-8")
        result = produce_verify_result(payload)
        assert result.passed is False
        assert result.record is not None
        assert any(
            item.result == RESULT_BROKEN and "该假设可能击穿 L0" in item.red_line
            for item in result.record.lines
        )
    finally:
        reset_default_claim_store()
