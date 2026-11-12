"""TicketWorkflow: one independently idempotent child run per implementation ticket."""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import CancelledError, is_cancelled_exception

from dev_pipeline.models import ReviewOverride, TicketInput, TicketResult
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows.ticket_steps import (
    TicketRuntime,
    finish_stopped,
    notify_parent,
    run_one_cycle,
    wait_until_eligible,
)


@workflow.defn
class TicketWorkflow:
    """Child loop: Pending → Implementing → Reviewing → (HumanGate) → Verifying."""

    def __init__(self) -> None:
        self._work = TicketRuntime()

    @workflow.run
    async def run(self, payload: TicketInput) -> TicketResult:
        """Execute one implementation ticket from Pending to a terminal stage.

        The child stays in Pending until declared dependencies are verified.
        Starting the child directly with unmet dependencies does not begin work.

        Args:
            payload: Child start input, including declared dependencies.

        Returns:
            The ticket's terminal or parked result.
        """
        work = self._work
        work.input = payload
        work.stage = TicketStage.PENDING
        work.dependencies_verified = not payload.depends_on
        try:
            await notify_parent(work)
            if not await wait_until_eligible(work):
                return await finish_stopped(work)
            work.move(TicketStage.IMPLEMENTING, "dependencies_satisfied")
            await notify_parent(work)
            while not work.stopped() and work.stage is not TicketStage.VERIFIED:
                await run_one_cycle(work)
            if work.stopped() and work.stage is not TicketStage.VERIFIED:
                return await finish_stopped(work)
            return work.result()
        except Exception as exc:
            if is_cancelled_exception(exc) or isinstance(exc, CancelledError):
                work.aborted = True
                return await finish_stopped(work)
            raise

    @workflow.signal
    def dependencies_satisfied(self) -> None:
        """Mark declared dependencies as verified so this ticket may start work."""
        self._work.dependencies_verified = True

    @workflow.signal
    def review_override(self, payload: ReviewOverride) -> None:
        """Apply a human review decision only while this ticket is in HumanGate."""
        if self._work.input is not None and payload.ticket_id != self._work.input.ticket_id:
            return
        if not self._work.in_stage(TicketStage.HUMAN_GATE):
            return
        self._work.override = payload

    @workflow.signal
    def retry_ticket(self) -> None:
        """Retry this ticket from Implementing after a failure or block."""
        self._work.retry_requested = True

    @workflow.signal
    def drop_ticket(self) -> None:
        """Remove this ticket from the Story list and stop work."""
        self._work.dropped = True

    @workflow.signal
    def abort(self) -> None:
        """Stop work because the parent Story was aborted."""
        self._work.aborted = True

    @workflow.query
    def stage(self) -> str:
        """Return the current child stage name."""
        return self._work.stage.value

    @workflow.query
    def snapshot(self) -> TicketResult:
        """Return a thin, queryable view of this ticket's progress."""
        work = self._work
        ticket_id = work.input.ticket_id if work.input is not None else ""
        execution_id = work.input.execution_id if work.input is not None else ""
        return TicketResult(
            ticket_id=ticket_id,
            execution_id=execution_id,
            stage=work.stage,
            detail=work.detail,
            sandbox=work.sandbox,
            deliverables=work.deliverables,
            review=work.review,
        )
