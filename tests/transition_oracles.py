"""Test oracles for legal/illegal transitions. Not used by production code."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dev_pipeline.models import TicketSnapshot
from dev_pipeline.stages import TERMINAL_FAILURE_TICKET_STAGES, TicketStage

# README / DP-9 expected parent arrows, used as an independent test oracle.
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
        ("Implementing", "TestDeploying", "authorize_test_deploy"),
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
    ("Implementing", "TestDeploying", "all_required_tickets_verified", "parent"),
    ("Verifying", "Retro", "all_required_tickets_verified", "parent"),
    ("Verifying", "TestDeploying", "authorize_test_deploy", "parent"),
    ("TestDeploying", "Done", "skip_retro", "parent"),
    ("Intake", "ProductionDeploying", "publish_production", "parent"),
    ("TestDeploying", "ProductionDeploying", "publish_production", "parent"),
    ("Reviewing", "Implementing", "blocking_auto_return", "child"),
    ("Reviewing", "Verifying", "blocking_or_paradigm_drift", "child"),
    ("Pending", "Reviewing", "skip_implementing", "child"),
    ("Pending", "Implementing", "dependencies_satisfied", "child"),
)


def ticket_views_from_stages(
    stages: Mapping[str, TicketStage],
    *,
    unhandled: Mapping[str, bool] | None = None,
    depends_on: Mapping[str, Sequence[str]] | None = None,
) -> tuple[TicketSnapshot, ...]:
    """Helper for table tests to build snapshot tuples from stage maps."""

    unhandled = unhandled or {}
    depends_on = depends_on or {}
    return tuple(
        TicketSnapshot(
            ticket_id=ticket_id,
            stage=stage,
            depends_on=list(depends_on.get(ticket_id, ())),
            started=stage is not TicketStage.PENDING,
            unhandled_failure=unhandled.get(
                ticket_id, stage in TERMINAL_FAILURE_TICKET_STAGES
            ),
        )
        for ticket_id, stage in stages.items()
    )
