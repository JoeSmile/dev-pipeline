"""Transition table tests: README / TestDeploying / DP-9, plus illegal paths."""

from __future__ import annotations

import pytest
from tests.transition_oracles import (
    ILLEGAL_EXAMPLES,
    README_PARENT_EDGES,
    ticket_views_from_stages,
)

from dev_pipeline.errors import IllegalTransition, ProductionDeployForbidden
from dev_pipeline.models import PublishRequest, TicketSpec
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.transitions import (
    ChildGuardContext,
    ParentGuardContext,
    assert_legal_transition,
    can_enter_retro,
    can_enter_test_deploying,
    child_edges,
    is_legal_transition,
    parent_edges,
    validate_publish_target,
)


def test_parent_table_contains_readme_and_test_deploying_edges() -> None:
    edges = parent_edges()
    missing = README_PARENT_EDGES - edges
    assert missing == frozenset(), f"转移表缺少 README/DP-9 边: {missing}"
    assert ("Implementing", "TestDeploying", "authorize_test_deploy") in edges
    assert ("TestDeploying", "Retro", "test_deploy_success") in edges
    assert ("TestDeploying", "Blocked", "test_deploy_failure") in edges
    assert ("Implementing", "Retro", "skip_test_deploy") not in edges
    assert ("Implementing", "TestDeploying", "all_required_tickets_verified") not in edges
    assert ("Verifying", "TestDeploying", "authorize_test_deploy") not in edges
    assert ("Verifying", "Retro", "all_required_tickets_verified") not in edges


def test_parent_table_has_no_production_deploy_transition() -> None:
    for source, dest, trigger in parent_edges():
        blob = f"{source} {dest} {trigger}".lower()
        assert "production" not in blob
        assert dest not in {"Production", "ProductionDeploying", "Prod"}
        assert "publish_production" not in trigger


def test_child_table_contains_core_loop() -> None:
    edges = child_edges()
    assert ("Pending", "Implementing", "dependencies_satisfied") in edges
    assert ("Implementing", "Reviewing", "diff_submitted") in edges
    assert ("Reviewing", "HumanGate", "blocking_or_paradigm_drift") in edges
    assert ("Reviewing", "Verifying", "advisory_only") in edges
    assert ("Verifying", "Verified", "verify_passed") in edges


@pytest.mark.parametrize(("source", "dest", "trigger", "scope"), ILLEGAL_EXAMPLES)
def test_illegal_examples_are_rejected(source: str, dest: str, trigger: str, scope: str) -> None:
    with pytest.raises((IllegalTransition, ProductionDeployForbidden)):
        assert_legal_transition(source, dest, trigger, scope=scope)  # type: ignore[arg-type]


def test_coding_before_lock_scope_or_scheme_is_rejected() -> None:
    for source in ("Intake", "ScopeLocked", "SchemeDecided"):
        assert not is_legal_transition(source, "Implementing", "start_eligible_children")
        assert not is_legal_transition(source, "Implementing", "start_coding")


def test_cannot_enter_test_deploying_while_sibling_reviewing() -> None:
    tickets = ticket_views_from_stages(
        {"A": TicketStage.VERIFIED, "B": TicketStage.REVIEWING}
    )
    assert can_enter_test_deploying(tickets) is False
    assert can_enter_retro(tickets, TestDeployStatus.SUCCEEDED) is False
    ctx = ParentGuardContext(tickets=tickets, test_deploy_status=TestDeployStatus.NOT_STARTED)
    with pytest.raises(IllegalTransition, match="不能进入 TestDeploying"):
        assert_legal_transition(
            ParentStage.IMPLEMENTING.value,
            ParentStage.TEST_DEPLOYING.value,
            "authorize_test_deploy",
            scope="parent",
            ctx=ctx,
        )
    with pytest.raises(IllegalTransition):
        assert_legal_transition(
            ParentStage.IMPLEMENTING.value,
            ParentStage.RETRO.value,
            "skip_test_deploy",
            scope="parent",
            ctx=ctx,
        )


def test_cannot_enter_retro_until_test_deploying_succeeded() -> None:
    tickets = ticket_views_from_stages({"A": TicketStage.VERIFIED})
    assert can_enter_test_deploying(tickets) is True
    assert can_enter_retro(tickets, TestDeployStatus.NOT_STARTED) is False
    assert can_enter_retro(tickets, TestDeployStatus.FAILED) is False
    assert can_enter_retro(tickets, TestDeployStatus.IN_PROGRESS) is False
    assert can_enter_retro(tickets, TestDeployStatus.SUCCEEDED) is True
    with pytest.raises(IllegalTransition, match="尚未成功"):
        assert_legal_transition(
            ParentStage.TEST_DEPLOYING.value,
            ParentStage.RETRO.value,
            "test_deploy_success",
            scope="parent",
            ctx=ParentGuardContext(
                tickets=tickets, test_deploy_status=TestDeployStatus.FAILED
            ),
        )


def test_unhandled_failure_blocks_test_deploying_and_retro() -> None:
    tickets = ticket_views_from_stages(
        {"A": TicketStage.FAILED, "B": TicketStage.VERIFIED}
    )
    assert can_enter_test_deploying(tickets) is False
    assert can_enter_retro(tickets, TestDeployStatus.SUCCEEDED) is False


def test_reject_blocked_or_rework_in_flight_blocks_retro() -> None:
    rejected = ticket_views_from_stages(
        {"A": TicketStage.BLOCKED, "B": TicketStage.VERIFIED}
    )
    assert can_enter_test_deploying(rejected) is False
    assert can_enter_retro(rejected, TestDeployStatus.SUCCEEDED) is False
    rework = ticket_views_from_stages(
        {"A": TicketStage.IMPLEMENTING, "B": TicketStage.VERIFIED},
        unhandled={"A": False, "B": False},
    )
    assert can_enter_test_deploying(rework) is False
    assert can_enter_retro(rework, TestDeployStatus.SUCCEEDED) is False


def test_dropped_ticket_does_not_block_test_deploying() -> None:
    tickets = ticket_views_from_stages(
        {"A": TicketStage.VERIFIED, "B": TicketStage.DROPPED},
        unhandled={"A": False, "B": False},
    )
    assert can_enter_test_deploying(tickets) is True


def test_pending_ticket_blocks_test_deploying() -> None:
    tickets = ticket_views_from_stages(
        {"A": TicketStage.VERIFIED, "B": TicketStage.PENDING}
    )
    assert can_enter_test_deploying(tickets) is False


def test_ticket_spec_has_no_deferred_flag() -> None:
    assert "required" not in TicketSpec.__dataclass_fields__
    spec = TicketSpec(ticket_id="A")
    assert not hasattr(spec, "required")


def test_child_cannot_enter_implementing_until_dependencies_verified() -> None:
    assert not is_legal_transition(
        "Pending", "Implementing", "dependencies_satisfied", scope="child"
    )
    with pytest.raises(IllegalTransition, match="依赖尚未验证"):
        assert_legal_transition(
            "Pending", "Implementing", "dependencies_satisfied", scope="child"
        )
    assert is_legal_transition(
        "Pending",
        "Implementing",
        "dependencies_satisfied",
        scope="child",
        ctx=ChildGuardContext(dependencies_verified=True),
    )


def test_production_publish_target_is_rejected() -> None:
    with pytest.raises(ProductionDeployForbidden, match="禁止生产发布"):
        validate_publish_target("production")
    with pytest.raises(ProductionDeployForbidden):
        validate_publish_target("prod")
    with pytest.raises(ProductionDeployForbidden):
        validate_publish_target("live")
    with pytest.raises(ProductionDeployForbidden):
        validate_publish_target("staging")
    assert validate_publish_target("test") == "test"
    request = PublishRequest(story_id="s", execution_id="e", target="production")  # type: ignore[arg-type]
    with pytest.raises(ProductionDeployForbidden):
        validate_publish_target(request.target)


def test_blocking_review_cannot_skip_human_gate() -> None:
    assert is_legal_transition(
        "Reviewing", "Verifying", "advisory_only", scope="child"
    )
    assert not is_legal_transition(
        "Reviewing", "Verifying", "blocking_or_paradigm_drift", scope="child"
    )
    assert is_legal_transition(
        "Reviewing", "HumanGate", "blocking_or_paradigm_drift", scope="child"
    )
