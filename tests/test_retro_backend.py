"""Filesystem tests for daily notes, candidate updates, and promotion writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.evidence_contract import UTF8_BOM, read_text_utf8
from dev_pipeline.retro import (
    CANDIDATE_STATUS,
    PROMOTION_TARGET_L0,
    REASON_AUTO_PROMOTE,
    REASON_INELIGIBLE,
    REASON_UNKNOWN_CANDIDATE,
    CandidateRecord,
    PromoteApplyInput,
    PromoteRuleRequest,
    RetroEvidenceLine,
    RetroInput,
    RetroTicketEvidence,
    is_difficulty_score_report,
    render_candidate_text,
)
from dev_pipeline.retro_backend import (
    produce_promote_result,
    produce_retro_result,
    refuse_auto_promote_in_retro,
    write_candidate,
)


def _payload(config_root: Path, story_id: str = "STORY-A") -> RetroInput:
    return RetroInput(
        story_id=story_id,
        execution_id="exec-1",
        config_root=str(config_root),
        daily_date="2026-11-12",
        written_at="2026-11-12T04:00:00Z",
        test_deploy_address="http://test.local/stories/STORY-A",
        test_deploy_status="succeeded",
        tickets=[
            RetroTicketEvidence(
                ticket_id="A",
                stage="Verified",
                verify_passed=True,
                verify_evidence_path="wt/A/evidence/verify.md",
                verify_lines=[
                    RetroEvidenceLine(
                        red_line="CHARTER 硬红线第 5 条",
                        result="守住",
                        evidence_path="wt/A/evidence/verify.md",
                    )
                ],
            )
        ],
    )


def test_retro_writes_utf8_daily_and_does_not_auto_promote(tmp_path: Path) -> None:
    result = produce_retro_result(_payload(tmp_path))
    assert result.recorded is True
    assert result.record is not None
    daily = Path(result.record.daily_path)
    assert daily.is_file()
    assert not daily.read_bytes().startswith(UTF8_BOM)
    text = read_text_utf8(daily)
    assert "STORY-A" in text
    assert "wt/A/evidence/verify.md" in text
    assert is_difficulty_score_report(text) is False
    assert "技术难度" not in text
    assert "业务难度" not in text
    assert result.record.candidates
    for item in result.record.candidates:
        assert item.status == CANDIDATE_STATUS
        path = tmp_path / "docs" / "knowledge" / "candidates" / f"{item.candidate_id}.md"
        assert path.is_file()
        body = path.read_text(encoding="utf-8")
        assert "状态: candidate" in body
        assert "L0" not in body.split("状态:", 1)[1].splitlines()[0]


def test_append_does_not_wipe_prior_daily_content(tmp_path: Path) -> None:
    first = produce_retro_result(_payload(tmp_path, "STORY-OLD"))
    assert first.record is not None
    daily = Path(first.record.daily_path)
    produce_retro_result(_payload(tmp_path, "STORY-NEW"))
    text = daily.read_text(encoding="utf-8")
    assert "STORY-OLD" in text
    assert "STORY-NEW" in text
    assert text.index("STORY-OLD") < text.index("STORY-NEW")


def test_auto_promote_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="禁止自动升级"):
        refuse_auto_promote_in_retro([], PROMOTION_TARGET_L0)
    seeded = CandidateRecord(candidate_id="seeded", title="已有候选", hits=2)
    write_candidate(tmp_path / "docs" / "knowledge" / "candidates", seeded)
    result = produce_retro_result(_payload(tmp_path))
    assert result.record is not None
    by_id = {item.candidate_id: item for item in result.record.candidates}
    assert by_id["seeded"].hits == 2
    assert by_id["seeded"].status == CANDIDATE_STATUS


def test_legal_promote_writes_and_illegal_does_not(tmp_path: Path) -> None:
    record = CandidateRecord(
        candidate_id="cand-1",
        title="可晋升规则",
        hits=2,
        citations=["evidence/verify.md"],
    )
    write_candidate(tmp_path / "docs" / "knowledge" / "candidates", record)
    legal = produce_promote_result(
        PromoteApplyInput(
            request=PromoteRuleRequest(
                candidate_id="cand-1",
                target="L0",
                reason="两次真实命中",
                evidence_citation="evidence/verify.md",
                decided_at="2026-11-12T04:00:00Z",
            ),
            config_root=str(tmp_path),
        )
    )
    assert legal.promoted is True
    assert legal.record is not None
    text = Path(legal.record.path).read_text(encoding="utf-8")
    assert "状态: L0" in text
    assert "两次真实命中" in text

    missing = produce_promote_result(
        PromoteApplyInput(
            request=PromoteRuleRequest(
                candidate_id="no-such",
                target="L0",
                reason="x",
                evidence_citation="cite",
            ),
            config_root=str(tmp_path),
        )
    )
    assert missing.promoted is False
    assert missing.reason == REASON_UNKNOWN_CANDIDATE

    weak = CandidateRecord(candidate_id="weak", title="不足", hits=1)
    write_candidate(tmp_path / "docs" / "knowledge" / "candidates", weak)
    ineligible = produce_promote_result(
        PromoteApplyInput(
            request=PromoteRuleRequest(
                candidate_id="weak",
                target="L0",
                reason="想跳过门槛",
                evidence_citation="cite",
            ),
            config_root=str(tmp_path),
        )
    )
    assert ineligible.promoted is False
    assert ineligible.reason == REASON_INELIGIBLE
    weak_text = (tmp_path / "docs" / "knowledge" / "candidates" / "weak.md").read_text(
        encoding="utf-8"
    )
    assert "状态: candidate" in weak_text

    auto = produce_promote_result(
        PromoteApplyInput(
            request=PromoteRuleRequest(
                candidate_id="cand-1",
                target="全局 Skill",
                reason="自动",
                evidence_citation="evidence/verify.md",
                automatic=True,
            ),
            config_root=str(tmp_path),
        )
    )
    assert auto.promoted is False
    assert REASON_AUTO_PROMOTE in auto.reason


def test_candidate_file_round_trip_utf8(tmp_path: Path) -> None:
    record = CandidateRecord(candidate_id="中文-id", title="中文标题", hits=1)
    path = write_candidate(tmp_path / "docs" / "knowledge" / "candidates", record)
    text = path.read_text(encoding="utf-8")
    assert "中文标题" in text
    assert render_candidate_text(record) == text
