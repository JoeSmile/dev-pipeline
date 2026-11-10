"""Unit tests for human-signal payload schemas (no Temporal worker required)."""

from __future__ import annotations

from dev_pipeline.models import (
    AbortRequest,
    ApproveSpikeRequest,
    AuthorizeTestDeployRequest,
    PromoteRuleRequest,
    ReopenRequest,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    TicketSpec,
)
from dev_pipeline.signal_schemas import parse_human_signal


def test_valid_payloads_and_mappings_are_accepted() -> None:
    parsed, reason = parse_human_signal("lock_scope", ScopeLock(in_scope="in"))
    assert reason is None
    assert isinstance(parsed, ScopeLock)
    parsed, reason = parse_human_signal("lock_scope", {"in_scope": "范围"})
    assert reason is None
    assert isinstance(parsed, ScopeLock) and parsed.in_scope == "范围"
    parsed, reason = parse_human_signal("approve_spike", {"accepted": True})
    assert reason is None
    assert isinstance(parsed, ApproveSpikeRequest) and parsed.accepted is True
    parsed, reason = parse_human_signal("authorize_test_deploy", None)
    assert reason is None
    assert isinstance(parsed, AuthorizeTestDeployRequest)


def test_missing_required_fields_and_type_errors_are_rejected() -> None:
    _, reason = parse_human_signal("lock_scope", {})
    assert reason is not None and "缺少必填字段" in reason
    _, reason = parse_human_signal("lock_scope", {"in_scope": "   "})
    assert reason is not None and "非空字符串" in reason
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
    assert reason is not None and "has_high_risk_assumption" in reason
    _, reason = parse_human_signal("review_override", {"ticket_id": "A", "decision": "maybe"})
    assert reason is not None and "approve、rework 或 reject" in reason
    _, reason = parse_human_signal("promote_rule", {"rule_name": "r", "automatic": "yes"})
    assert reason is not None and "必须是 bool" in reason


def test_decide_scheme_tickets_must_have_ids() -> None:
    _, reason = parse_human_signal(
        "decide_scheme",
        {"has_high_risk_assumption": False, "tickets": [{"depends_on": []}]},
    )
    assert reason is not None and "ticket_id" in reason
    parsed, reason = parse_human_signal(
        "decide_scheme",
        {
            "has_high_risk_assumption": False,
            "tickets": [TicketSpec(ticket_id="T-1")],
        },
    )
    assert reason is None
    assert isinstance(parsed, SchemeDecision)
    assert parsed.tickets[0].ticket_id == "T-1"


def test_review_override_and_reopen_accept_valid_objects() -> None:
    parsed, reason = parse_human_signal(
        "review_override", ReviewOverride(ticket_id="A", decision="approve")
    )
    assert reason is None and isinstance(parsed, ReviewOverride)
    parsed, reason = parse_human_signal("reopen", ReopenRequest(execution_id="exec-2"))
    assert reason is None and isinstance(parsed, ReopenRequest)
    parsed, reason = parse_human_signal(
        "promote_rule", PromoteRuleRequest(rule_name="skill", automatic=False)
    )
    assert reason is None and isinstance(parsed, PromoteRuleRequest)
