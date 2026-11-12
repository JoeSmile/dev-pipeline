"""Pure unit tests for Story Retro helpers: dates, eligibility, auto-promote."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dev_pipeline.retro import (
    CANDIDATE_STATUS,
    PROMOTION_TARGET_GLOBAL_SKILL,
    PROMOTION_TARGET_L0,
    REASON_AUTO_PROMOTE,
    REASON_INELIGIBLE,
    CandidateRecord,
    PromoteRuleRequest,
    RetroEvidenceLine,
    RetroHypothesis,
    RetroInput,
    RetroTicketEvidence,
    apply_candidate_observation,
    apply_human_promotion,
    collect_story_observations,
    daily_note_date,
    is_difficulty_score_report,
    is_eligible_for_promotion,
    merge_observations,
    normalize_promotion_target,
    parse_candidate_text,
    reject_automatic_promotion,
    render_candidate_text,
    render_daily_note,
    render_story_section,
)


def test_daily_note_date_uses_asia_shanghai_not_os_local() -> None:
    utc = datetime(2026, 11, 12, 16, 0, 0, tzinfo=UTC)
    assert daily_note_date(utc) == "2026-11-13"
    before = datetime(2026, 11, 12, 15, 59, 0, tzinfo=UTC)
    assert daily_note_date(before) == "2026-11-12"
    shanghai = datetime(2026, 11, 12, 23, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert daily_note_date(shanghai) == "2026-11-12"


def test_eligibility_requires_two_hits_or_one_hard_near_miss() -> None:
    none = CandidateRecord(candidate_id="c", hits=1, hard_near_misses=0)
    assert is_eligible_for_promotion(none) is False
    hits = CandidateRecord(candidate_id="c", hits=2, hard_near_misses=0)
    assert is_eligible_for_promotion(hits) is True
    near = CandidateRecord(candidate_id="c", hits=0, hard_near_misses=1)
    assert is_eligible_for_promotion(near) is True


def test_observation_increments_hits_but_does_not_promote() -> None:
    seed = CandidateRecord(candidate_id="c", title="红线", hits=1)
    updated = apply_candidate_observation(seed, hits_delta=1, citation="evidence/verify.md")
    assert updated.hits == 2
    assert is_eligible_for_promotion(updated) is True
    assert updated.status == CANDIDATE_STATUS
    assert updated.status not in {PROMOTION_TARGET_L0, PROMOTION_TARGET_GLOBAL_SKILL}


def test_automatic_promotion_is_rejected() -> None:
    record = CandidateRecord(candidate_id="c", hits=2)
    request = PromoteRuleRequest(
        candidate_id="c",
        target="L0",
        reason="自动",
        evidence_citation="cite",
        automatic=True,
    )
    updated, reason = apply_human_promotion(record, request)
    assert updated is None
    assert reason == reject_automatic_promotion()
    assert REASON_AUTO_PROMOTE in (reason or "")


def test_ineligible_human_promotion_is_rejected() -> None:
    record = CandidateRecord(candidate_id="c", hits=1)
    request = PromoteRuleRequest(
        candidate_id="c",
        target="L0",
        reason="想跳过门槛",
        evidence_citation="cite",
    )
    updated, reason = apply_human_promotion(record, request)
    assert updated is None
    assert reason == REASON_INELIGIBLE
    assert record.status == CANDIDATE_STATUS


def test_legal_human_promotion_updates_status() -> None:
    record = CandidateRecord(candidate_id="c", title="规则", hits=2)
    request = PromoteRuleRequest(
        candidate_id="c",
        target="全局 Skill",
        reason="两次真实命中",
        evidence_citation="docs/knowledge/candidates/c.md",
        decided_at="2026-11-12T10:00:00Z",
    )
    updated, reason = apply_human_promotion(record, request)
    assert reason is None
    assert updated is not None
    assert updated.status == PROMOTION_TARGET_GLOBAL_SKILL
    assert updated.promotion_reason == "两次真实命中"
    assert updated.promoted_at == "2026-11-12T10:00:00Z"


def test_normalize_promotion_target() -> None:
    assert normalize_promotion_target("L0") == "L0"
    assert normalize_promotion_target("l0") == "L0"
    assert normalize_promotion_target("全局 Skill") == "全局 Skill"
    assert normalize_promotion_target("global_skill") == "全局 Skill"
    assert normalize_promotion_target("production") is None


def test_daily_note_is_not_difficulty_score_and_cites_evidence() -> None:
    payload = RetroInput(
        story_id="STORY-1",
        execution_id="exec-1",
        config_root="/tmp",
        daily_date="2026-11-12",
        written_at="2026-11-12T04:00:00Z",
        test_deploy_address="http://test.local/stories/STORY-1",
        test_deploy_status="succeeded",
        tickets=[
            RetroTicketEvidence(
                ticket_id="A",
                stage="Verified",
                verify_passed=True,
                verify_evidence_path="wt/evidence/verify.md",
                verify_lines=[
                    RetroEvidenceLine(
                        red_line="CHARTER 第 5 条",
                        result="守住",
                        evidence_path="wt/evidence/verify.md",
                    )
                ],
            )
        ],
        hypotheses=[RetroHypothesis(statement="可复用的局部约定", grade="候选")],
    )
    observations = collect_story_observations(payload)
    candidates = merge_observations({}, observations)
    assert candidates
    assert all(item.status == CANDIDATE_STATUS for item in candidates)
    section = render_story_section(payload, candidates)
    assert is_difficulty_score_report(section) is False
    assert "技术难度" not in section
    assert "业务难度" not in section
    assert "wt/evidence/verify.md" in section
    assert "待证" not in [line for line in section.splitlines() if "CHARTER" in line][0]
    existing = "# 2026-11-12 日记\n\n## 先前的 Story\n\n- 保留这段。\n"
    combined = render_daily_note(existing, payload, candidates)
    assert "先前的 Story" in combined
    assert "保留这段" in combined
    assert "STORY-1" in combined


def test_uncited_claim_is_marked_pending() -> None:
    payload = RetroInput(
        story_id="STORY-2",
        execution_id="exec-2",
        config_root="/tmp",
        daily_date="2026-11-12",
        tickets=[RetroTicketEvidence(ticket_id="A", stage="Verified", verify_passed=True)],
    )
    section = render_story_section(payload, [])
    assert "待证" in section


def test_candidate_round_trip() -> None:
    record = CandidateRecord(
        candidate_id="red-line-abc",
        title="红线",
        hits=2,
        citations=["evidence/verify.md"],
    )
    parsed = parse_candidate_text(render_candidate_text(record))
    assert parsed.candidate_id == "red-line-abc"
    assert parsed.hits == 2
    assert parsed.status == CANDIDATE_STATUS
    assert parsed.citations == ["evidence/verify.md"]
