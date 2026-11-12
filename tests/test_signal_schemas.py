"""Unit tests for human-signal payload schemas (no Temporal worker required)."""

from __future__ import annotations

from datetime import UTC, datetime

from dev_pipeline.models import (
    RETRIEVAL_STUB,
    AbortRequest,
    ApproveSpikeRequest,
    AuthorizeTestDeployRequest,
    ReopenRequest,
    ReviewOverride,
    SchemeDecision,
    SchemeHypothesis,
    ScopeLock,
    TicketSpec,
    format_scope_lock_time,
    has_red_line_hypothesis,
    is_complete_scheme_decision,
    is_complete_scope_lock,
)
from dev_pipeline.retro import PromoteRuleRequest
from dev_pipeline.signal_schemas import parse_human_signal


def test_valid_payloads_and_mappings_are_accepted() -> None:
    parsed, reason = parse_human_signal(
        "lock_scope",
        ScopeLock(in_scope="in", out_of_scope="out", locked_by="joe"),
    )
    assert reason is None
    assert isinstance(parsed, ScopeLock)
    assert parsed.out_of_scope == "out"
    assert parsed.locked_by == "joe"
    assert parsed.locked_at == ""
    parsed, reason = parse_human_signal(
        "lock_scope",
        {"in_scope": "范围", "out_of_scope": "非目标", "locked_by": "joe"},
    )
    assert reason is None
    assert isinstance(parsed, ScopeLock) and parsed.in_scope == "范围"
    assert parsed.out_of_scope == "非目标"
    assert parsed.locked_by == "joe"
    parsed, reason = parse_human_signal("approve_spike", {"accepted": True})
    assert reason is None
    assert isinstance(parsed, ApproveSpikeRequest) and parsed.accepted is True
    parsed, reason = parse_human_signal("authorize_test_deploy", None)
    assert reason is None
    assert isinstance(parsed, AuthorizeTestDeployRequest)


def test_missing_required_fields_and_type_errors_are_rejected() -> None:
    _, reason = parse_human_signal("lock_scope", {})
    assert reason is not None and "缺少必填字段" in reason
    _, reason = parse_human_signal("lock_scope", {"in_scope": "范围"})
    assert reason is not None and "缺少必填字段" in reason
    assert "out_of_scope" in reason and "locked_by" in reason
    _, reason = parse_human_signal("lock_scope", {"in_scope": "范围", "out_of_scope": "非目标"})
    assert reason is not None and "缺少必填字段" in reason and "locked_by" in reason
    _, reason = parse_human_signal(
        "lock_scope",
        {"in_scope": "范围", "out_of_scope": "   ", "locked_by": "joe"},
    )
    assert reason is not None and "非空字符串" in reason
    _, reason = parse_human_signal("lock_scope", {"in_scope": "   "})
    assert reason is not None
    _, reason = parse_human_signal("lock_scope", "not-an-object")
    assert reason is not None and "类型无效" in reason
    _, reason = parse_human_signal("approve_spike", True)
    assert reason is not None and "类型无效" in reason
    _, reason = parse_human_signal("approve_spike", {"accepted": "yes"})
    assert reason is not None and "必须是 bool" in reason
    _, reason = parse_human_signal("abort", {"reason": ""})
    assert reason is not None
    _, reason = parse_human_signal("reopen", AbortRequest(reason="x"))
    assert reason is not None and "类型无效" in reason
    _, reason = parse_human_signal("decide_scheme", {"notes": "only"})
    assert reason is not None and "缺少必填字段" in reason
    assert "tradeoff" in reason and "hypotheses" in reason and "decided_by" in reason
    _, reason = parse_human_signal(
        "review_override",
        {"ticket_id": "A", "decision": "maybe", "decided_by": "tester"},
    )
    assert reason is not None and "放行、打回、否决" in reason
    _, reason = parse_human_signal("review_override", {"ticket_id": "A", "decision": "approve"})
    assert reason is not None and "缺少必填字段" in reason and "decided_by" in reason
    _, reason = parse_human_signal(
        "promote_rule",
        {
            "candidate_id": "r",
            "target": "L0",
            "reason": "已有两次命中",
            "evidence_citation": "daily.md",
            "automatic": "yes",
        },
    )
    assert reason is not None and "必须是 bool" in reason


def test_decide_scheme_tickets_must_have_ids() -> None:
    base = {
        "tradeoff": "采用 A 方案",
        "hypotheses": [{"statement": "局部可替换", "grade": "局部"}],
        "decided_by": "郭伟",
    }
    _, reason = parse_human_signal(
        "decide_scheme",
        {**base, "tickets": [{"depends_on": []}]},
    )
    assert reason is not None and "ticket_id" in reason
    parsed, reason = parse_human_signal(
        "decide_scheme",
        {**base, "tickets": [TicketSpec(ticket_id="T-1")]},
    )
    assert reason is None
    assert isinstance(parsed, SchemeDecision)
    assert parsed.tickets[0].ticket_id == "T-1"
    assert parsed.tradeoff == "采用 A 方案"
    assert parsed.decided_by == "郭伟"
    assert parsed.hypotheses[0].grade == "局部"
    assert parsed.has_high_risk_assumption is False
    assert parsed.retrieval == RETRIEVAL_STUB
    assert parsed.decided_at == ""


def test_decide_scheme_rejects_unknown_grade_and_risk_conflict() -> None:
    _, reason = parse_human_signal(
        "decide_scheme",
        {
            "tradeoff": "采用 A 方案",
            "hypotheses": [{"statement": "未知风险", "grade": "高危"}],
            "decided_by": "郭伟",
        },
    )
    assert reason is not None and "红线级、局部或候选" in reason
    _, reason = parse_human_signal(
        "decide_scheme",
        {
            "tradeoff": "采用 A 方案",
            "hypotheses": [{"statement": "局部可替换", "grade": "局部"}],
            "decided_by": "郭伟",
            "has_high_risk_assumption": True,
        },
    )
    assert reason is not None and "与假设分级不一致" in reason
    parsed, reason = parse_human_signal(
        "decide_scheme",
        SchemeDecision(
            tradeoff="采用 A 方案",
            hypotheses=[SchemeHypothesis(statement="可能击穿 L0", grade="红线级")],
            decided_by="郭伟",
            decided_at="1999-01-01T00:00:00Z",
            retrieval="真实命中",
        ),
    )
    assert reason is None
    assert isinstance(parsed, SchemeDecision)
    assert parsed.has_high_risk_assumption is True
    assert parsed.decided_at == ""
    assert parsed.retrieval == RETRIEVAL_STUB


def test_red_line_helper_and_complete_scheme_record() -> None:
    incomplete = SchemeDecision(tradeoff="取舍", decided_by="郭伟")
    assert has_red_line_hypothesis(incomplete.hypotheses) is False
    assert is_complete_scheme_decision(incomplete) is False
    complete = SchemeDecision(
        tradeoff="取舍",
        hypotheses=[SchemeHypothesis(statement="可能击穿 L0", grade="红线级")],
        decided_by="郭伟",
        decided_at="2026-11-12T05:53:00Z",
        retrieval=RETRIEVAL_STUB,
        has_high_risk_assumption=True,
    )
    assert has_red_line_hypothesis(complete.hypotheses) is True
    assert is_complete_scheme_decision(complete) is True


def test_review_override_and_reopen_accept_valid_objects() -> None:
    parsed, reason = parse_human_signal(
        "review_override",
        ReviewOverride(
            ticket_id="A",
            decision="approve",
            decided_by="tester",
            decided_at="1999-01-01T00:00:00Z",
        ),
    )
    assert reason is None and isinstance(parsed, ReviewOverride)
    assert parsed.decision == "approve"
    assert parsed.decided_by == "tester"
    assert parsed.decided_at == ""
    parsed, reason = parse_human_signal(
        "review_override",
        {"ticket_id": "A", "decision": "放行", "decided_by": "郭伟", "reason": "可接受"},
    )
    assert reason is None and isinstance(parsed, ReviewOverride)
    assert parsed.decision == "approve"
    assert parsed.decided_by == "郭伟"
    assert parsed.reason == "可接受"
    parsed, reason = parse_human_signal(
        "review_override",
        {"ticket_id": "B", "decision": "打回", "decided_by": "郭伟"},
    )
    assert reason is None and isinstance(parsed, ReviewOverride)
    assert parsed.decision == "rework"
    parsed, reason = parse_human_signal(
        "review_override",
        {"ticket_id": "C", "decision": "否决", "decided_by": "郭伟"},
    )
    assert reason is None and isinstance(parsed, ReviewOverride)
    assert parsed.decision == "reject"
    parsed, reason = parse_human_signal("reopen", ReopenRequest(execution_id="exec-2"))
    assert reason is None and isinstance(parsed, ReopenRequest)
    parsed, reason = parse_human_signal(
        "promote_rule",
        PromoteRuleRequest(
            candidate_id="skill",
            target="L0",
            reason="已有两次真实命中",
            evidence_citation="docs/knowledge/candidates/skill.md",
            automatic=False,
        ),
    )
    assert reason is None and isinstance(parsed, PromoteRuleRequest)
    assert parsed.candidate_id == "skill"
    assert parsed.target == "L0"
    parsed, reason = parse_human_signal(
        "promote_rule",
        {
            "候选标识": "skill-2",
            "晋升目标": "全局 Skill",
            "理由": "一次硬近失",
            "证据引用": "docs/knowledge/candidates/skill-2.md",
        },
    )
    assert reason is None and isinstance(parsed, PromoteRuleRequest)
    assert parsed.candidate_id == "skill-2"
    assert parsed.target == "全局 Skill"
    _, reason = parse_human_signal("promote_rule", {"candidate_id": "x"})
    assert reason is not None and "缺少必填字段" in reason
    _, reason = parse_human_signal(
        "promote_rule",
        {
            "candidate_id": "x",
            "target": "production",
            "reason": "no",
            "evidence_citation": "cite",
        },
    )
    assert reason is not None and "L0 或 全局 Skill" in reason


def test_format_scope_lock_time_is_utc_zulu() -> None:
    moment = datetime(2026, 11, 12, 5, 53, 0, tzinfo=UTC)
    assert format_scope_lock_time(moment) == "2026-11-12T05:53:00Z"
    naive = datetime(2026, 11, 12, 5, 53, 0)
    assert format_scope_lock_time(naive) == "2026-11-12T05:53:00Z"


def test_is_complete_scope_lock_requires_all_record_fields() -> None:
    incomplete = ScopeLock(in_scope="in", out_of_scope="out", locked_by="joe")
    assert is_complete_scope_lock(incomplete) is False
    complete = ScopeLock(
        in_scope="in",
        out_of_scope="out",
        locked_by="joe",
        locked_at="2026-11-12T05:53:00Z",
    )
    assert is_complete_scope_lock(complete) is True


def test_submit_tickets_requires_complete_items_and_submitter() -> None:
    from dev_pipeline.models import TicketListSubmission

    parsed, reason = parse_human_signal(
        "submit_tickets",
        {
            "submitted_by": "joe",
            "tickets": [
                {
                    "ticket_id": "T-1",
                    "goal": "实现接口",
                    "acceptance": "单测通过",
                    "depends_on": [],
                    "io_contract": "输入 Story，输出 diff",
                }
            ],
        },
    )
    assert reason is None
    assert isinstance(parsed, TicketListSubmission)
    assert parsed.submitted_by == "joe"
    assert parsed.tickets[0].ticket_id == "T-1"
    _, reason = parse_human_signal("submit_tickets", {"tickets": []})
    assert reason is not None and "submitted_by" in reason
    _, reason = parse_human_signal(
        "submit_tickets",
        {"submitted_by": "joe", "tickets": [{"ticket_id": "T-1"}]},
    )
    assert reason is not None
    _, reason = parse_human_signal(
        "submit_tickets",
        {"submitted_by": "joe", "tickets": "not-a-list"},
    )
    assert reason is not None
