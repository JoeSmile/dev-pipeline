"""Data-driven legal transitions for parent and child stages.

This module is side-effect free so tests can assert the table without a worker.
Workflows must use these helpers rather than inventing ad-hoc moves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.errors import IllegalTransition, ProductionDeployForbidden
from dev_pipeline.models import TicketSnapshot
from dev_pipeline.stages import (
    IN_FLIGHT_TICKET_STAGES,
    TERMINAL_FAILURE_TICKET_STAGES,
    TestDeployStatus,
    TicketStage,
)

Scope = Literal["parent", "child"]

# Triggers that would imply a production publish path. None are legal.
_PRODUCTION_TOKENS = ("production", "prod", "live")


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
    Transition("Spike", "TicketsReady", "approve_spike_accept", "parent", doc="最小验证过"),
    Transition("Spike", "Blocked", "approve_spike_reject", "parent", doc="拒绝高危假设"),
    Transition("Spike", "Blocked", "red_line_fail", "parent", doc="击穿红线且无防护"),
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
        "all_required_tickets_verified",
        "parent",
        guard="all_required_tickets_verified",
        doc="全部必做 Ticket 验证过线",
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


def validate_publish_target(target: str) -> str:
    """Only the test environment may be published to."""

    reject_production_path(target)
    normalized = target.strip().lower()
    if normalized != "test":
        raise ProductionDeployForbidden(
            f"禁止生产发布（以及任何非测试环境发布）。已拒绝目标: {target}，仅允许 target=test"
        )
    return normalized


def can_enter_test_deploying(tickets: Sequence[TicketSnapshot]) -> bool:
    """Parent may enter TestDeploying only when every required ticket is verified."""

    required = [ticket for ticket in tickets if ticket.required]
    for ticket in required:
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

    return (
        test_deploy_status is TestDeployStatus.SUCCEEDED and can_enter_test_deploying(tickets)
    )


def required_unhandled_failures(tickets: Sequence[TicketSnapshot]) -> tuple[TicketSnapshot, ...]:
    """Required tickets that have failed or blocked and have not been dropped."""

    return tuple(
        ticket
        for ticket in tickets
        if ticket.required
        and (ticket.unhandled_failure or ticket.stage in TERMINAL_FAILURE_TICKET_STAGES)
    )


def _guard_allows(transition: Transition, ctx: ParentGuardContext | None) -> bool:
    if transition.guard is None:
        return True
    if ctx is None:
        return False
    if transition.guard == "all_required_tickets_verified":
        return can_enter_test_deploying(ctx.tickets)
    if transition.guard == "test_deploy_succeeded":
        return can_enter_retro(ctx.tickets, ctx.test_deploy_status)
    if transition.guard == "dependencies_verified":
        return True
    return False


def find_transition(
    source: str,
    dest: str,
    trigger: str,
    *,
    scope: Scope | None = None,
) -> Transition | None:
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
    ctx: ParentGuardContext | None = None,
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
    ctx: ParentGuardContext | None = None,
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
        if found.guard == "all_required_tickets_verified":
            reason = (
                "尚有必做 Ticket 未验证过线、仍在 Implementing/Reviewing/HumanGate/Verifying，"
                "或失败尚未处理，不能进入 TestDeploying 或 Retro"
            )
        elif found.guard == "test_deploy_succeeded":
            reason = "TestDeploying 尚未成功，不能进入 Retro"
        raise IllegalTransition(source, dest, trigger, reason)
    return found


def parent_edges() -> frozenset[tuple[str, str, str]]:
    return frozenset((item.source, item.dest, item.trigger) for item in PARENT_TRANSITIONS)


def child_edges() -> frozenset[tuple[str, str, str]]:
    return frozenset((item.source, item.dest, item.trigger) for item in CHILD_TRANSITIONS)


def legal_destinations(
    source: str,
    trigger: str,
    *,
    scope: Scope | None = None,
    ctx: ParentGuardContext | None = None,
) -> tuple[str, ...]:
    dests: list[str] = []
    for item in LEGAL_TRANSITIONS:
        if item.source != source or item.trigger != trigger:
            continue
        if scope is not None and item.scope != scope:
            continue
        if _guard_allows(item, ctx):
            dests.append(item.dest)
    return tuple(dests)


# README / DP-26 expected parent arrows, used as an independent test oracle.
README_PARENT_EDGES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("Intake", "ScopeLocked", "lock_scope"),
        ("ScopeLocked", "SchemeDecided", "decide_scheme"),
        ("SchemeDecided", "Spike", "high_risk_assumption"),
        ("SchemeDecided", "TicketsReady", "no_high_risk_assumption"),
        ("Spike", "TicketsReady", "approve_spike_accept"),
        ("Spike", "Blocked", "approve_spike_reject"),
        ("Spike", "Blocked", "red_line_fail"),
        ("TicketsReady", "Implementing", "start_eligible_children"),
        ("Implementing", "TestDeploying", "all_required_tickets_verified"),
        ("TestDeploying", "Retro", "test_deploy_success"),
        ("TestDeploying", "Blocked", "test_deploy_failure"),
        ("Retro", "Done", "retro_written"),
    }
)

# Explicit illegal examples that tests must keep rejected.
ILLEGAL_EXAMPLES: tuple[tuple[str, str, str, str], ...] = (
    ("Intake", "Implementing", "start_coding", "parent"),
    ("Intake", "TicketsReady", "skip_gates", "parent"),
    ("ScopeLocked", "Implementing", "start_coding", "parent"),
    ("SchemeDecided", "Implementing", "start_coding", "parent"),
    ("SchemeDecided", "TestDeploying", "skip_gates", "parent"),
    ("TicketsReady", "Retro", "skip_test_deploy", "parent"),
    ("TicketsReady", "TestDeploying", "skip_children", "parent"),
    ("Implementing", "Retro", "skip_test_deploy", "parent"),
    ("Implementing", "Done", "skip_gates", "parent"),
    ("Verifying", "Retro", "all_required_tickets_verified", "parent"),
    ("TestDeploying", "Done", "skip_retro", "parent"),
    ("Intake", "ProductionDeploying", "publish_production", "parent"),
    ("TestDeploying", "ProductionDeploying", "publish_production", "parent"),
    ("Reviewing", "Implementing", "blocking_auto_return", "child"),
    ("Reviewing", "Verifying", "blocking_or_paradigm_drift", "child"),
    ("Pending", "Reviewing", "skip_implementing", "child"),
)


def ticket_views_from_stages(
    stages: Mapping[str, TicketStage],
    *,
    required: Mapping[str, bool] | None = None,
    unhandled: Mapping[str, bool] | None = None,
    depends_on: Mapping[str, Sequence[str]] | None = None,
) -> tuple[TicketSnapshot, ...]:
    """Helper for table tests to build snapshot tuples from stage maps."""

    required = required or {}
    unhandled = unhandled or {}
    depends_on = depends_on or {}
    return tuple(
        TicketSnapshot(
            ticket_id=ticket_id,
            stage=stage,
            required=required.get(ticket_id, True),
            depends_on=list(depends_on.get(ticket_id, ())),
            started=stage is not TicketStage.PENDING,
            unhandled_failure=unhandled.get(
                ticket_id, stage in TERMINAL_FAILURE_TICKET_STAGES
            ),
        )
        for ticket_id, stage in stages.items()
    )
