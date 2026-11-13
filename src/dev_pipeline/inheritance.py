"""Read-only reopen inheritance types and pure builders. Workflow-safe."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from dev_pipeline.models import (
    SchemeDecision,
    SchemeHypothesis,
    ScopeLock,
    is_complete_scheme_decision,
    is_complete_scope_lock,
)
from dev_pipeline.stages import TicketStage
from dev_pipeline.verify import VerifyRecord

# Review / retro / promotion records are not listed on DP-30 and stay 待证.
UNLISTED_INHERITANCE_PENDING = "待证"


@dataclass
class ClosedTicketEvidence:
    """Read-only evidence from a ticket already closed in the old execution."""

    ticket_id: str
    stage: str
    verify: VerifyRecord | None = None
    evidence_path: str = ""
    detail: str = ""


@dataclass
class InheritedArtifacts:
    """Read-only artifacts a new execution may inherit after reopen.

    Writable control (``review_override``, ``promote_rule``, auto-promotion)
    is never included. Spike *records* are not inherited; spike hypotheses
    already present on the scheme travel with ``hypotheses``.
    """

    scope: ScopeLock | None = None
    scheme: SchemeDecision | None = None
    hypotheses: list[SchemeHypothesis] = field(default_factory=list)
    closed_ticket_evidence: list[ClosedTicketEvidence] = field(default_factory=list)


_CLOSED_TICKET_STAGES = frozenset({TicketStage.VERIFIED, TicketStage.DROPPED})


def copy_scope_lock(scope: ScopeLock | None) -> ScopeLock | None:
    """Return a complete scope lock copy, or None when inheritance must skip it."""

    if scope is None or not is_complete_scope_lock(scope):
        return None
    return ScopeLock(
        in_scope=scope.in_scope,
        out_of_scope=scope.out_of_scope,
        locked_by=scope.locked_by,
        locked_at=scope.locked_at,
    )


def copy_scheme_decision(scheme: SchemeDecision | None) -> SchemeDecision | None:
    """Return a complete scheme copy, including hypotheses, or None."""

    if scheme is None or not is_complete_scheme_decision(scheme):
        return None
    return SchemeDecision(
        tradeoff=scheme.tradeoff,
        hypotheses=[
            SchemeHypothesis(statement=item.statement, grade=item.grade)
            for item in scheme.hypotheses
        ],
        decided_by=scheme.decided_by,
        decided_at=scheme.decided_at,
        retrieval=scheme.retrieval,
        tickets=list(scheme.tickets),
        has_high_risk_assumption=scheme.has_high_risk_assumption,
        notes=scheme.notes,
    )


def copy_verify_record(record: VerifyRecord | None) -> VerifyRecord | None:
    """Return a shallow dataclass copy of verify evidence, or None."""

    if record is None:
        return None
    return replace(record, lines=list(record.lines))


def closed_ticket_evidence_from_snapshots(
    tickets: list[object],
) -> list[ClosedTicketEvidence]:
    """Collect verify evidence from Verified or Dropped ticket snapshots."""

    collected: list[ClosedTicketEvidence] = []
    for item in tickets:
        stage = getattr(item, "stage", None)
        if stage not in _CLOSED_TICKET_STAGES:
            continue
        verify = copy_verify_record(getattr(item, "verify", None))
        if verify is None and stage is TicketStage.DROPPED:
            continue
        collected.append(
            ClosedTicketEvidence(
                ticket_id=str(getattr(item, "ticket_id", "")),
                stage=stage.value if isinstance(stage, TicketStage) else str(stage),
                verify=verify,
                evidence_path="" if verify is None else verify.evidence_path,
                detail="" if verify is None else verify.reason,
            )
        )
    return collected


def build_inherited_artifacts(
    *,
    scope: ScopeLock | None,
    scheme: SchemeDecision | None,
    tickets: list[object],
    spike_statement: str = "",
) -> InheritedArtifacts:
    """Build the allowed read-only inheritance payload for a new execution.

    ``spike_statement`` is ignored as a new hypothesis when it is already
    present on the scheme. The spike conclusion record itself is not copied.
    """

    copied_scope = copy_scope_lock(scope)
    copied_scheme = copy_scheme_decision(scheme)
    hypotheses: list[SchemeHypothesis] = []
    if copied_scheme is not None:
        hypotheses = [
            SchemeHypothesis(statement=item.statement, grade=item.grade)
            for item in copied_scheme.hypotheses
        ]
    del spike_statement
    return InheritedArtifacts(
        scope=copied_scope,
        scheme=copied_scheme,
        hypotheses=hypotheses,
        closed_ticket_evidence=closed_ticket_evidence_from_snapshots(tickets),
    )


def coalesce_inherited(
    inherited: InheritedArtifacts | None,
    inherited_scope: ScopeLock | None,
) -> InheritedArtifacts | None:
    """Fill scope from the DP-16 field when a richer payload omitted it."""

    scope = copy_scope_lock(inherited_scope)
    if inherited is None:
        if scope is None:
            return None
        return InheritedArtifacts(scope=scope)
    if inherited.scope is None and scope is not None:
        return InheritedArtifacts(
            scope=scope,
            scheme=inherited.scheme,
            hypotheses=list(inherited.hypotheses),
            closed_ticket_evidence=list(inherited.closed_ticket_evidence),
        )
    return inherited
