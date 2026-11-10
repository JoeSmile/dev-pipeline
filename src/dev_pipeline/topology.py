"""Dependency-topology helpers: who may start, and who must wait.

Pure functions, safe to call from workflows and from table tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from dev_pipeline.models import TicketSnapshot, TicketSpec
from dev_pipeline.stages import TicketStage


def index_specs(specs: Sequence[TicketSpec]) -> dict[str, TicketSpec]:
    """Index ticket specs by id, rejecting a duplicate ticket_id."""

    indexed = {spec.ticket_id: spec for spec in specs}
    if len(indexed) != len(specs):
        raise ValueError("Ticket 清单中存在重复的 ticket_id")
    return indexed


def is_eligible_to_start(
    spec: TicketSpec,
    stages: Mapping[str, TicketStage],
    *,
    already_started: bool,
) -> bool:
    """A ticket may start only when every dependency has reached Verified."""

    if already_started:
        return False
    current = stages.get(spec.ticket_id, TicketStage.PENDING)
    if current is not TicketStage.PENDING:
        return False
    return all(stages.get(dep) is TicketStage.VERIFIED for dep in spec.depends_on)


def eligible_ticket_ids(
    specs: Sequence[TicketSpec],
    snapshots: Sequence[TicketSnapshot],
) -> tuple[str, ...]:
    """Return tickets that may be started now, in stable spec order."""

    index_specs(specs)
    by_id = {item.ticket_id: item for item in snapshots}
    stages = {item.ticket_id: item.stage for item in snapshots}
    eligible: list[str] = []
    for spec in specs:
        snap = by_id.get(spec.ticket_id)
        if snap is not None and snap.stage is TicketStage.DROPPED:
            continue
        already_started = bool(snap.started) if snap is not None else False
        if snap is not None and snap.stage is not TicketStage.PENDING:
            already_started = True
        if is_eligible_to_start(spec, stages, already_started=already_started):
            eligible.append(spec.ticket_id)
    return tuple(eligible)


def would_start_early(spec: TicketSpec, stages: Mapping[str, TicketStage]) -> bool:
    """True when a dependent ticket would start before its dependencies are verified."""

    if not spec.depends_on:
        return False
    return not all(stages.get(dep) is TicketStage.VERIFIED for dep in spec.depends_on)
