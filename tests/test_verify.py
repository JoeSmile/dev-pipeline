"""Unit tests for the DP-32 evidence contract and Verifying classification."""

from __future__ import annotations

from dev_pipeline.verify import (
    CITATION_REPRODUCIBLE_EVIDENCE,
    FIELD_COMMAND,
    FIELD_EVIDENCE_PATH,
    FIELD_EXECUTION_ID,
    FIELD_RED_LINE,
    FIELD_RESULT,
    FIELD_TICKET_ID,
    REASON_MISSING_MAPPING,
    REASON_PROBE_NOT_PASS,
    REASON_RED_LINE_BROKEN,
    RESULT_BROKEN,
    RESULT_DRY_RUN,
    RESULT_HELD,
    RESULT_HISTORY_ONLY,
    RESULT_PENDING,
    VERIFY_MODE_DRY_RUN,
    VERIFY_MODE_HISTORY,
    VerifyEvidenceLine,
    VerifyRecord,
    classify_verify_outcome,
    is_probe_only_command,
    parse_evidence_text,
    render_evidence_text,
)


def _line(result: str, command: str = "python -c 'print(1)' ；exit=0") -> VerifyEvidenceLine:
    return VerifyEvidenceLine(
        red_line=CITATION_REPRODUCIBLE_EVIDENCE,
        command_or_phenomenon=command,
        result=result,
        evidence_path="/tmp/tree/evidence/verify.md",
    )


def test_held_mapping_passes_and_broken_line_fails() -> None:
    passed, reason = classify_verify_outcome([_line(RESULT_HELD)])
    assert passed is True
    assert "过线" in reason
    passed, reason = classify_verify_outcome([_line(RESULT_HELD), _line(RESULT_BROKEN)])
    assert passed is False
    assert reason == REASON_RED_LINE_BROKEN


def test_missing_held_line_does_not_pass() -> None:
    passed, reason = classify_verify_outcome([_line(RESULT_PENDING)])
    assert passed is False
    assert reason == REASON_MISSING_MAPPING
    passed, reason = classify_verify_outcome([])
    assert passed is False
    assert reason == REASON_MISSING_MAPPING


def test_dry_run_or_history_only_cannot_pass() -> None:
    held = _line(RESULT_HELD)
    passed, reason = classify_verify_outcome([held], mode=VERIFY_MODE_DRY_RUN)
    assert passed is False
    assert reason == REASON_PROBE_NOT_PASS
    passed, reason = classify_verify_outcome([held], mode=VERIFY_MODE_HISTORY)
    assert passed is False
    assert reason == REASON_PROBE_NOT_PASS
    dry = _line(RESULT_DRY_RUN, command="dry-run")
    passed, reason = classify_verify_outcome([dry])
    assert passed is False
    assert reason == REASON_PROBE_NOT_PASS
    history = _line(RESULT_HISTORY_ONLY, command="Temporal History 存在")
    passed, reason = classify_verify_outcome([history])
    assert passed is False
    assert reason == REASON_PROBE_NOT_PASS
    assert is_probe_only_command("仅 History 存在") is True
    assert is_probe_only_command("python -c 'print(1)' ；exit=0") is False


def test_evidence_round_trip_keeps_utf8_min_fields_and_chinese() -> None:
    path = "D:/authorized/T1/exec-1/evidence/verify.md"
    record = VerifyRecord(
        ticket_id="T1",
        execution_id="exec-1",
        story_id="STORY-1",
        passed=True,
        evidence_path=path,
        reason="过线证据齐",
        lines=[
            VerifyEvidenceLine(
                red_line=CITATION_REPRODUCIBLE_EVIDENCE,
                command_or_phenomenon="python -c \"print('红线 ↔ 证据')\" ；exit=0",
                result=RESULT_HELD,
                evidence_path=path,
            )
        ],
    )
    text = render_evidence_text(record)
    encoded = text.encode("utf-8")
    assert encoded.decode("utf-8") == text
    assert "红线 ↔ 证据" in text
    assert FIELD_RED_LINE in text
    assert FIELD_COMMAND in text
    assert FIELD_RESULT in text
    assert FIELD_EVIDENCE_PATH in text
    assert FIELD_TICKET_ID in text
    assert FIELD_EXECUTION_ID in text
    parsed = parse_evidence_text(text)
    assert parsed.ticket_id == "T1"
    assert parsed.execution_id == "exec-1"
    assert parsed.passed is True
    assert parsed.lines[0].red_line == CITATION_REPRODUCIBLE_EVIDENCE
    assert "红线 ↔ 证据" in parsed.lines[0].command_or_phenomenon
    assert parsed.lines[0].result == RESULT_HELD
    assert parsed.lines[0].evidence_path == path
