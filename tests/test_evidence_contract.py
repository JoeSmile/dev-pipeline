"""Tests for the DP-32 evidence-on-disk contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.evidence_contract import (
    CONTRACT_SAMPLE_TEXT,
    DEFAULT_EVIDENCE_RELPATH,
    FIELD_COMMAND,
    FIELD_EVIDENCE_PATH,
    FIELD_EXECUTION_ID,
    FIELD_RED_LINE,
    FIELD_RESULT,
    FIELD_TICKET_ID,
    TEXT_ENCODING,
    UTF8_BOM,
    VERIFY_MIN_FIELDS,
    decode_utf8,
    encode_utf8,
    evidence_relpath,
    missing_verify_min_fields,
    read_text_utf8,
    write_text_utf8,
)
from dev_pipeline.verify import (
    RESULT_BROKEN,
    RESULT_HELD,
    VerifyEvidenceLine,
    VerifyRecord,
    render_evidence_text,
)


def _record(result: str, *, passed: bool, path: str = DEFAULT_EVIDENCE_RELPATH) -> VerifyRecord:
    return VerifyRecord(
        ticket_id="T1",
        execution_id="exec-1",
        story_id="STORY-1",
        passed=passed,
        evidence_path=path,
        reason="过线证据齐" if passed else "未守住红线，该 Ticket 回到 Implementing",
        lines=[
            VerifyEvidenceLine(
                red_line="CHARTER 硬红线第 5 条：任务对错以可复现证据为准",
                command_or_phenomenon=f"python -c \"print('{CONTRACT_SAMPLE_TEXT}')\" ；exit=0",
                result=result,
                evidence_path=path,
            )
        ],
    )


def test_pass_and_fail_evidence_contain_minimum_fields() -> None:
    for result, passed in ((RESULT_HELD, True), (RESULT_BROKEN, False)):
        text = render_evidence_text(_record(result, passed=passed))
        assert missing_verify_min_fields(text) == ()
        assert FIELD_RED_LINE in text
        assert FIELD_COMMAND in text
        assert FIELD_RESULT in text
        assert FIELD_EVIDENCE_PATH in text
        assert FIELD_TICKET_ID in text
        assert FIELD_EXECUTION_ID in text
        assert result in text


def test_encode_utf8_keeps_chinese_and_symbols_without_bom() -> None:
    raw = encode_utf8(CONTRACT_SAMPLE_TEXT)
    assert not raw.startswith(UTF8_BOM)
    assert decode_utf8(raw) == CONTRACT_SAMPLE_TEXT
    assert "红线" in CONTRACT_SAMPLE_TEXT
    assert "↔" in CONTRACT_SAMPLE_TEXT
    assert "「" in CONTRACT_SAMPLE_TEXT
    assert "」" in CONTRACT_SAMPLE_TEXT
    assert "—" in CONTRACT_SAMPLE_TEXT
    # ↔ is outside GBK; a code-page write would fail or replace the symbol.
    with pytest.raises(UnicodeEncodeError):
        CONTRACT_SAMPLE_TEXT.encode("gbk")


def test_write_text_utf8_ignores_legacy_stdio_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp936")
    monkeypatch.setenv("PYTHONUTF8", "0")
    path = tmp_path / "evidence" / "verify.md"
    text = render_evidence_text(_record(RESULT_HELD, passed=True, path=str(path)))
    written = Path(write_text_utf8(path, text))
    raw = written.read_bytes()
    assert not raw.startswith(UTF8_BOM)
    assert raw == encode_utf8(text)
    assert decode_utf8(raw) == text
    assert CONTRACT_SAMPLE_TEXT in read_text_utf8(written)
    assert missing_verify_min_fields(read_text_utf8(written)) == ()
    assert raw.decode(TEXT_ENCODING) == text


def test_write_text_utf8_is_not_rewritten_as_gbk(tmp_path: Path) -> None:
    path = tmp_path / "spot-check.md"
    write_text_utf8(path, CONTRACT_SAMPLE_TEXT)
    raw = path.read_bytes()
    assert raw == CONTRACT_SAMPLE_TEXT.encode("utf-8")
    # Mis-decoding UTF-8 as GBK/cp936 must not be treated as the contract text.
    mojibake = raw.decode("gbk", errors="replace")
    assert mojibake != CONTRACT_SAMPLE_TEXT
    assert "↔" not in mojibake


def test_read_text_utf8_strips_bom_but_writes_are_bomless(tmp_path: Path) -> None:
    path = tmp_path / "bom.md"
    write_text_utf8(path, CONTRACT_SAMPLE_TEXT)
    assert not path.read_bytes().startswith(UTF8_BOM)
    path.write_bytes(UTF8_BOM + encode_utf8(CONTRACT_SAMPLE_TEXT))
    assert read_text_utf8(path) == CONTRACT_SAMPLE_TEXT


def test_evidence_relpath_defaults_to_contract_path() -> None:
    assert evidence_relpath("") == DEFAULT_EVIDENCE_RELPATH
    assert evidence_relpath("   ") == DEFAULT_EVIDENCE_RELPATH
    assert evidence_relpath("notes/custom.md") == "notes/custom.md"


def test_verify_min_fields_tuple_matches_named_constants() -> None:
    assert VERIFY_MIN_FIELDS == (
        FIELD_RED_LINE,
        FIELD_COMMAND,
        FIELD_RESULT,
        FIELD_EVIDENCE_PATH,
        FIELD_TICKET_ID,
        FIELD_EXECUTION_ID,
    )
    assert missing_verify_min_fields("ticket_id: T1\n") == (
        FIELD_RED_LINE,
        FIELD_COMMAND,
        FIELD_RESULT,
        FIELD_EVIDENCE_PATH,
        FIELD_EXECUTION_ID,
    )


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "evidence.md"
    write_text_utf8(path, "对应红线: 示例\n")
    assert path.is_file()
