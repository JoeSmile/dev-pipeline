"""Mutable parent-run state and snapshot helpers. Must stay deterministic."""

from __future__ import annotations

from dev_pipeline.models import (
    PipelineResult,
    PipelineSnapshot,
    SchemeDecision,
    ScopeLock,
    StoryInput,
    TicketSnapshot,
    TicketSpec,
)
from dev_pipeline.stages import IN_FLIGHT_TICKET_STAGES, ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.transitions import ParentGuardContext, assert_legal_transition


class ParentState:
    """Queryable state for one DevPipelineWorkflow execution."""

    def __init__(self) -> None:
        self.stage = ParentStage.INTAKE
        self.waiting: str | None = "lock_scope"
        self.input: StoryInput | None = None
        self.scope: ScopeLock | None = None
        self.scheme: SchemeDecision | None = None
        self.spike_accepted: bool | None = None
        self.abort_requested = False
        self.abort_reason = "abort"
        self.blocked_reason: str | None = None
        self.last_illegal: str | None = None
        self.promote_status: str | None = None
        self.test_deploy_status = TestDeployStatus.NOT_STARTED
        self.test_deploy_address: str | None = None
        self.test_deploy_authorized = False
        self.tickets: dict[str, TicketSnapshot] = {}
        self.specs: list[TicketSpec] = []
        self.ticket_event = False

    def install_tickets(self, specs: list[TicketSpec]) -> None:
        """Replace the ticket list from a start payload or scheme decision."""
        if not specs:
            return
        self.specs = list(specs)
        self.tickets = {
            spec.ticket_id: TicketSnapshot(
                ticket_id=spec.ticket_id,
                stage=TicketStage.PENDING,
                depends_on=list(spec.depends_on),
                started=False,
            )
            for spec in specs
        }
        self.ticket_event = True

    def ticket_list(self) -> list[TicketSnapshot]:
        """Return ticket snapshots in the installed spec order."""
        return [
            self.tickets[spec.ticket_id]
            for spec in self.specs
            if spec.ticket_id in self.tickets
        ]

    def guard_ctx(self) -> ParentGuardContext:
        """Build the parent guard context from the current snapshot."""
        return ParentGuardContext(
            tickets=tuple(self.ticket_list()),
            test_deploy_status=self.test_deploy_status,
        )

    def has_in_flight(self) -> bool:
        """Return True when a non-dropped ticket is still in a working stage."""
        return any(
            not item.unhandled_failure and item.stage in IN_FLIGHT_TICKET_STAGES
            for item in self.ticket_list()
        )

    def move(self, dest: ParentStage, trigger: str) -> None:
        """Apply a legal parent transition, or no-op when already at dest."""
        if self.stage is dest:
            return
        assert_legal_transition(
            self.stage.value,
            dest.value,
            trigger,
            scope="parent",
            ctx=self.guard_ctx(),
        )
        self.stage = dest

    def record_illegal(self, message: str) -> None:
        """Record an operator-visible illegal request on the snapshot."""
        self.last_illegal = message

    def finish_blocked(self, reason: str) -> PipelineResult:
        """Mark the parent Blocked and return the terminal result."""
        self.stage = ParentStage.BLOCKED
        self.waiting = None
        self.blocked_reason = reason
        return self.result()

    def result(self) -> PipelineResult:
        """Build the terminal parent result from current state."""
        assert self.input is not None
        return PipelineResult(
            story_id=self.input.story_id,
            execution_id=self.input.execution_id,
            stage=self.stage,
            test_deploy_status=self.test_deploy_status,
            blocked_reason=self.blocked_reason,
            test_deploy_address=self.test_deploy_address,
        )

    def snapshot(self) -> PipelineSnapshot:
        """Build the queryable progress snapshot."""
        story_id = self.input.story_id if self.input is not None else ""
        execution_id = self.input.execution_id if self.input is not None else ""
        return PipelineSnapshot(
            story_id=story_id,
            execution_id=execution_id,
            stage=self.stage,
            waiting_signal=self.waiting,
            tickets=self.ticket_list(),
            test_deploy_status=self.test_deploy_status,
            blocked_reason=self.blocked_reason,
            last_illegal=self.last_illegal,
            promote_rule_status=self.promote_status,
            test_deploy_address=self.test_deploy_address,
        )
