"""Data-driven legal transitions for parent and child stages.

This module is side-effect free so tests can assert the table without a worker.
Workflows must use these helpers rather than inventing ad-hoc moves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.constants import ALLOWED_PUBLISH_TARGETS, TEST_PUBLISH_TARGET
from dev_pipeline.errors import IllegalTransition, ProductionDeployForbidden
from dev_pipeline.models import PublishTarget, TicketSnapshot
from dev_pipeline.stages import (
    IN_FLIGHT_TICKET_STAGES,
    TERMINAL_FAILURE_TICKET_STAGES,
    TestDeployStatus,
    TicketStage,
)

Scope = Literal["parent", "child"]


@dataclass(frozen=True)
class Transition:
    """One legal arrow in the stage machine."""

    source: str
    dest: str
    trigger: str
    scope: Scope
    guard: str | None = None
    doc: str = ""


@dataclass(frozen=True)
class ParentGuardContext:
    """Facts the parent guards need, independent of Temporal."""

    tickets: tuple[TicketSnapshot, ...] = field(default_factory=tuple)
    test_deploy_status: TestDeployStatus = TestDeployStatus.NOT_STARTED


@dataclass(frozen=True)
class ChildGuardContext:
    """Facts the child guards need, independent of Temporal."""

    dependencies_verified: bool = False


GuardContext = ParentGuardContext | ChildGuardContext


PARENT_TRANSITIONS: tuple[Transition, ...] = (
    Transition("Intake", "ScopeLocked", "lock_scope", "parent", doc="范围+非目标已锁"),
    Transition("ScopeLocked", "SchemeDecided", "decide_scheme", "parent", doc="方案取舍已拍"),
    Transition(
        "SchemeDecided",
        "Spike",
        "high_risk_assumption",
        "parent",
        doc="假设可能击穿 L0",
    ),
    Transition(
        "SchemeDecided",
        "TicketsReady",
        "no_high_risk_assumption",
        "parent",
        doc="无高危假设",
    ),
    Transition("Spike", "TicketsReady", "spike_verified", "parent", doc="验证过"),
    Transition("Spike", "TicketsReady", "approve_spike_accept", "parent", doc="有防护且批准继续"),
    Transition("Spike", "Blocked", "approve_spike_reject", "parent", doc="拒绝高危假设"),
    Transition("Spike", "Blocked", "red_line_fail", "parent", doc="击穿红线且无防护"),
    Transition("Spike", "Blocked", "spike_overrun", "parent", doc="超时未验证，按无防护处理"),
    Transition(
        "TicketsReady",
        "Implementing",
        "start_eligible_children",
        "parent",
        doc="派 Coding Worker（Child）",
    ),
    Transition(
        "Implementing",
        "TestDeploying",
        "authorize_test_deploy",
        "parent",
        guard="all_tickets_verified",
        doc="全部未移除 Ticket 验证过线，且已授权测试发布",
    ),
    Transition(
        "TestDeploying",
        "Retro",
        "test_deploy_success",
        "parent",
        guard="test_deploy_succeeded",
        doc="测试环境发布成功",
    ),
    Transition(
        "TestDeploying",
        "Blocked",
        "test_deploy_failure",
        "parent",
        doc="测试部署失败，不得进入 Retro",
    ),
    Transition("Retro", "Done", "retro_written", "parent", doc="daily+candidate 已记录（桩）"),
    Transition("Intake", "Blocked", "abort", "parent"),
    Transition("ScopeLocked", "Blocked", "abort", "parent"),
    Transition("SchemeDecided", "Blocked", "abort", "parent"),
    Transition("Spike", "Blocked", "abort", "parent"),
    Transition("TicketsReady", "Blocked", "abort", "parent"),
    Transition("Implementing", "Blocked", "abort", "parent"),
    Transition("TestDeploying", "Blocked", "abort", "parent"),
    Transition("Retro", "Blocked", "abort", "parent"),
)

# Child loop lives on TicketWorkflow. Reviewing cannot skip HumanGate on blocking findings.
CHILD_TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        "Pending",
        "Implementing",
        "dependencies_satisfied",
        "child",
        guard="dependencies_verified",
        doc="依赖已过线，允许开工",
    ),
    Transition("Implementing", "Reviewing", "diff_submitted", "child"),
    Transition(
        "Reviewing",
        "Verifying",
        "advisory_only",
        "child",
        doc="仅建议/信息，可直接 Verifying",
    ),
    Transition(
        "Reviewing",
        "HumanGate",
        "blocking_or_paradigm_drift",
        "child",
        doc="阻断项或范式漂移必须 HumanGate",
    ),
    Transition("HumanGate", "Implementing", "review_override_rework", "child", doc="人改完续跑"),
    Transition("HumanGate", "Verifying", "review_override_approve", "child", doc="人放行"),
    Transition("HumanGate", "Blocked", "review_override_reject", "child", doc="人否决"),
    Transition("Verifying", "Implementing", "verify_failed", "child", doc="验证未守住红线"),
    Transition("Verifying", "Verified", "verify_passed", "child", doc="过线证据齐"),
    Transition("Implementing", "Failed", "activity_failed", "child"),
    Transition("Reviewing", "Failed", "activity_failed", "child"),
    Transition("Verifying", "Failed", "activity_failed", "child"),
    Transition("Failed", "Implementing", "retry_ticket", "child"),
    Transition("Blocked", "Implementing", "retry_ticket", "child"),
    Transition("Failed", "Dropped", "drop_ticket", "child"),
    Transition("Blocked", "Dropped", "drop_ticket", "child"),
    Transition("Pending", "Dropped", "drop_ticket", "child"),
    Transition("Implementing", "Dropped", "drop_ticket", "child"),
    Transition("Reviewing", "Dropped", "drop_ticket", "child"),
    Transition("HumanGate", "Dropped", "drop_ticket", "child"),
    Transition("Verifying", "Dropped", "drop_ticket", "child"),
    Transition("Pending", "Dropped", "abort", "child"),
    Transition("Implementing", "Dropped", "abort", "child"),
    Transition("Reviewing", "Dropped", "abort", "child"),
    Transition("HumanGate", "Dropped", "abort", "child"),
    Transition("Verifying", "Dropped", "abort", "child"),
    Transition("Failed", "Dropped", "abort", "child"),
    Transition("Blocked", "Dropped", "abort", "child"),
)

LEGAL_TRANSITIONS: tuple[Transition, ...] = PARENT_TRANSITIONS + CHILD_TRANSITIONS


def _looks_like_production(token: str) -> bool:
    lowered = token.strip().lower().replace("_", "").replace("-", "")
    return any(part in lowered for part in ("production", "prod", "live"))


def reject_production_path(target: str, *, source: str = "", trigger: str = "") -> None:
    """Reject any production publish target or production-named transition."""

    candidates = (target, source, trigger)
    if any(_looks_like_production(item) for item in candidates if item):
        label = target or trigger or source
        raise ProductionDeployForbidden(
            f"禁止生产发布（以及任何非测试环境发布路径）。已拒绝: {label}"
        )


def validate_publish_target(target: str) -> PublishTarget:
    """Only the test environment may be published to."""

    reject_production_path(target)
    normalized = target.strip().lower()
    if normalized not in ALLOWED_PUBLISH_TARGETS:
        raise ProductionDeployForbidden(
            f"禁止生产发布（以及任何非测试环境发布）。已拒绝目标: {target}，"
            f"仅允许 target={TEST_PUBLISH_TARGET}"
        )
    return normalized


def _active_tickets(tickets: Sequence[TicketSnapshot]) -> tuple[TicketSnapshot, ...]:
    return tuple(ticket for ticket in tickets if ticket.stage is not TicketStage.DROPPED)


def can_enter_test_deploying(tickets: Sequence[TicketSnapshot]) -> bool:
    """Parent may enter TestDeploying only when every non-dropped ticket is verified."""

    for ticket in _active_tickets(tickets):
        if ticket.unhandled_failure:
            return False
        if ticket.stage in IN_FLIGHT_TICKET_STAGES:
            return False
        if ticket.stage is TicketStage.PENDING:
            return False
        if ticket.stage in TERMINAL_FAILURE_TICKET_STAGES:
            return False
        if ticket.stage is not TicketStage.VERIFIED:
            return False
    return True


def can_enter_retro(
    tickets: Sequence[TicketSnapshot],
    test_deploy_status: TestDeployStatus,
) -> bool:
    """Retro is legal only after a successful TestDeploying and verified tickets."""

    return test_deploy_status is TestDeployStatus.SUCCEEDED and can_enter_test_deploying(tickets)


def unhandled_failures(tickets: Sequence[TicketSnapshot]) -> tuple[TicketSnapshot, ...]:
    """Non-dropped tickets that have failed or blocked and have not been retried."""

    return tuple(
        ticket
        for ticket in _active_tickets(tickets)
        if ticket.unhandled_failure or ticket.stage in TERMINAL_FAILURE_TICKET_STAGES
    )


def _guard_allows(transition: Transition, ctx: GuardContext | None) -> bool:
    if transition.guard is None:
        return True
    if ctx is None:
        return False
    if transition.guard == "all_tickets_verified":
        return isinstance(ctx, ParentGuardContext) and can_enter_test_deploying(ctx.tickets)
    if transition.guard == "test_deploy_succeeded":
        return isinstance(ctx, ParentGuardContext) and can_enter_retro(
            ctx.tickets, ctx.test_deploy_status
        )
    if transition.guard == "dependencies_verified":
        return isinstance(ctx, ChildGuardContext) and ctx.dependencies_verified
    return False


def find_transition(
    source: str,
    dest: str,
    trigger: str,
    *,
    scope: Scope | None = None,
) -> Transition | None:
    """Return the matching legal transition, or None if the arrow does not exist."""

    for item in LEGAL_TRANSITIONS:
        if item.source == source and item.dest == dest and item.trigger == trigger:
            if scope is None or item.scope == scope:
                return item
    return None


def is_legal_transition(
    source: str,
    dest: str,
    trigger: str,
    *,
    scope: Scope | None = None,
    ctx: GuardContext | None = None,
) -> bool:
    """Return whether the arrow exists and its guard (if any) is satisfied."""

    try:
        reject_production_path(dest, source=source, trigger=trigger)
    except ProductionDeployForbidden:
        return False
    found = find_transition(source, dest, trigger, scope=scope)
    if found is None:
        return False
    return _guard_allows(found, ctx)


def assert_legal_transition(
    source: str,
    dest: str,
    trigger: str,
    *,
    scope: Scope | None = None,
    ctx: GuardContext | None = None,
) -> Transition:
    """Return the matching transition or raise an operator-visible error."""

    reject_production_path(dest, source=source, trigger=trigger)
    found = find_transition(source, dest, trigger, scope=scope)
    if found is None:
        raise IllegalTransition(
            source,
            dest,
            trigger,
            "该路径不在合法转移表中（禁止跳过门禁，也不存在生产部署转移）",
        )
    if not _guard_allows(found, ctx):
        reason = "当前守卫条件不满足"
        if found.guard == "all_tickets_verified":
            reason = (
                "尚有 Ticket 未验证过线、仍在 Implementing/Reviewing/HumanGate/Verifying，"
                "或失败尚未处理，不能进入 TestDeploying 或 Retro"
            )
        elif found.guard == "test_deploy_succeeded":
            reason = "TestDeploying 尚未成功，不能进入 Retro"
        elif found.guard == "dependencies_verified":
            reason = "依赖尚未验证过线，不能进入 Implementing"
        raise IllegalTransition(source, dest, trigger, reason)
    return found


def parent_edges() -> frozenset[tuple[str, str, str]]:
    """Return the parent legal table as (source, dest, trigger) tuples."""

    return frozenset((item.source, item.dest, item.trigger) for item in PARENT_TRANSITIONS)


def child_edges() -> frozenset[tuple[str, str, str]]:
    """Return the child legal table as (source, dest, trigger) tuples."""

    return frozenset((item.source, item.dest, item.trigger) for item in CHILD_TRANSITIONS)
