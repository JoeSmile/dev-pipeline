"""TicketWorkflow: one independently idempotent child run per implementation ticket."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from dev_pipeline.models import (
    ReviewOverride,
    TicketInput,
    TicketResult,
    TicketStateUpdate,
    TicketWorkInput,
    VerifyResult,
)
from dev_pipeline.stages import TicketStage
from dev_pipeline.transitions import assert_legal_transition

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.stubs import implement_ticket, review_ticket, verify_ticket

_ACTIVITY_TIMEOUT = timedelta(seconds=60)
_NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class TicketWorkflow:
    """Child loop: Implementing → Reviewing → (HumanGate) → Verifying."""

    def __init__(self) -> None:
        self._stage = TicketStage.IMPLEMENTING
        self._input: TicketInput | None = None
        self._override: ReviewOverride | None = None
        self._retry_requested = False
        self._dropped = False
        self._detail = ""

    @workflow.run
    async def run(self, payload: TicketInput) -> TicketResult:
        self._input = payload
        self._stage = TicketStage.IMPLEMENTING
        await self._notify_parent()
        while not self._dropped:
            try:
                if self._in(TicketStage.FAILED, TicketStage.BLOCKED):
                    await self._wait_for_retry_or_drop()
                    if self._dropped:
                        break
                    self._move(TicketStage.IMPLEMENTING, "retry_ticket")
                    await self._notify_parent()

                if self._in(TicketStage.IMPLEMENTING):
                    await self._run_implement()
                    if self._dropped or self._in(TicketStage.FAILED):
                        continue

                if self._in(TicketStage.REVIEWING):
                    await self._run_review()
                    if self._dropped or self._in(
                        TicketStage.FAILED,
                        TicketStage.BLOCKED,
                        TicketStage.IMPLEMENTING,
                    ):
                        continue

                if self._in(TicketStage.HUMAN_GATE):
                    await self._run_human_gate()
                    if self._dropped or self._in(
                        TicketStage.FAILED,
                        TicketStage.BLOCKED,
                        TicketStage.IMPLEMENTING,
                    ):
                        continue

                if self._in(TicketStage.VERIFYING):
                    if await self._run_verify():
                        self._move(TicketStage.VERIFIED, "verify_passed")
                        await self._notify_parent()
                        return self._result()
                    if self._dropped:
                        break
                    self._move(TicketStage.IMPLEMENTING, "verify_failed")
                    await self._notify_parent()
            except ActivityError as exc:
                self._detail = str(exc)
                if self._stage not in {TicketStage.FAILED, TicketStage.BLOCKED}:
                    self._move(TicketStage.FAILED, "activity_failed")
                await self._notify_parent(unhandled_failure=True)

        self._move(TicketStage.DROPPED, "drop_ticket")
        await self._notify_parent()
        return self._result()

    async def _run_implement(self) -> None:
        result = await workflow.execute_activity(
            implement_ticket,
            self._work_input(),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_NO_RETRY,
        )
        if self._dropped:
            return
        if not result.diff_submitted:
            self._detail = result.detail or "implement did not submit a diff"
            self._move(TicketStage.FAILED, "activity_failed")
            await self._notify_parent(unhandled_failure=True)
            return
        self._detail = result.detail
        self._move(TicketStage.REVIEWING, "diff_submitted")
        await self._notify_parent()

    async def _run_review(self) -> None:
        findings = await workflow.execute_activity(
            review_ticket,
            self._work_input(),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_NO_RETRY,
        )
        if self._dropped:
            return
        if findings.requires_human_gate():
            self._move(TicketStage.HUMAN_GATE, "blocking_or_paradigm_drift")
            await self._notify_parent()
            return
        self._move(TicketStage.VERIFYING, "advisory_only")
        await self._notify_parent()

    async def _run_human_gate(self) -> None:
        await workflow.wait_condition(lambda: self._override is not None or self._dropped)
        if self._dropped:
            return
        decision = self._override.decision if self._override is not None else "rework"
        self._override = None
        if decision == "reject":
            self._detail = "review_override reject"
            self._move(TicketStage.BLOCKED, "review_override_reject")
            await self._notify_parent(unhandled_failure=True)
            return
        if decision == "rework":
            self._move(TicketStage.IMPLEMENTING, "review_override_rework")
            await self._notify_parent()
            return
        self._move(TicketStage.VERIFYING, "review_override_approve")
        await self._notify_parent()

    async def _run_verify(self) -> bool:
        result: VerifyResult = await workflow.execute_activity(
            verify_ticket,
            self._work_input(),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_NO_RETRY,
        )
        if self._dropped:
            return False
        self._detail = result.evidence
        return result.passed

    async def _wait_for_retry_or_drop(self) -> None:
        self._retry_requested = False
        await workflow.wait_condition(lambda: self._retry_requested or self._dropped)
        self._retry_requested = False

    def _in(self, *stages: TicketStage) -> bool:
        return self._stage in stages

    def _move(self, dest: TicketStage, trigger: str) -> None:
        assert_legal_transition(self._stage.value, dest.value, trigger, scope="child")
        self._stage = dest

    def _work_input(self) -> TicketWorkInput:
        assert self._input is not None
        return TicketWorkInput(
            ticket_id=self._input.ticket_id,
            execution_id=self._input.execution_id,
            story_id=self._input.story_id,
        )

    def _result(self) -> TicketResult:
        assert self._input is not None
        return TicketResult(
            ticket_id=self._input.ticket_id,
            execution_id=self._input.execution_id,
            stage=self._stage,
            detail=self._detail,
        )

    async def _notify_parent(self, *, unhandled_failure: bool = False) -> None:
        assert self._input is not None
        handle = workflow.get_external_workflow_handle(self._input.parent_workflow_id)
        await handle.signal(
            "ticket_state_changed",
            TicketStateUpdate(
                ticket_id=self._input.ticket_id,
                stage=self._stage,
                unhandled_failure=unhandled_failure
                or self._stage in {TicketStage.FAILED, TicketStage.BLOCKED},
                detail=self._detail,
            ),
        )

    @workflow.signal
    def review_override(self, payload: ReviewOverride) -> None:
        if self._input is not None and payload.ticket_id != self._input.ticket_id:
            return
        self._override = payload

    @workflow.signal
    def retry_ticket(self) -> None:
        self._retry_requested = True

    @workflow.signal
    def drop_ticket(self) -> None:
        self._dropped = True

    @workflow.query
    def stage(self) -> str:
        return self._stage.value

    @workflow.query
    def snapshot(self) -> TicketResult:
        ticket_id = self._input.ticket_id if self._input is not None else ""
        execution_id = self._input.execution_id if self._input is not None else ""
        return TicketResult(
            ticket_id=ticket_id,
            execution_id=execution_id,
            stage=self._stage,
            detail=self._detail,
        )
