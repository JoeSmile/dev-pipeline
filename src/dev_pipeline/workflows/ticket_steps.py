"""Stage-step helpers for TicketWorkflow. Must stay deterministic."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception

from dev_pipeline.models import (
    ReviewOverride,
    SandboxRecord,
    SandboxWriteInput,
    TicketDeliverables,
    TicketInput,
    TicketResult,
    TicketStateUpdate,
    TicketWorkInput,
    VerifyResult,
    is_complete_deliverables,
)
from dev_pipeline.stages import TicketStage
from dev_pipeline.transitions import ChildGuardContext, assert_legal_transition

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.sandbox import apply_sandbox_write
    from dev_pipeline.activities.stubs import implement_ticket, review_ticket, verify_ticket
    from dev_pipeline.activities.workspace import claim_ticket_workspace

ACTIVITY_TIMEOUT = timedelta(seconds=60)
IMPLEMENT_TIMEOUT = timedelta(seconds=90)
CLAIM_TIMEOUT = timedelta(seconds=10)
HEARTBEAT_TIMEOUT = timedelta(seconds=2)
IMPLEMENT_HEARTBEAT_TIMEOUT = timedelta(seconds=30)
NO_RETRY = RetryPolicy(maximum_attempts=1)
CLAIM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=5),
)


class TicketRuntime:
    """Mutable state for one TicketWorkflow run. Not a Temporal workflow entry."""

    def __init__(self) -> None:
        self.stage = TicketStage.PENDING
        self.input: TicketInput | None = None
        self.override: ReviewOverride | None = None
        self.retry_requested = False
        self.dropped = False
        self.aborted = False
        self.dependencies_verified = False
        self.detail = ""
        self.sandbox: SandboxRecord | None = None
        self.deliverables: TicketDeliverables | None = None

    def stopped(self) -> bool:
        """Return True when the child should leave the work loop."""
        return self.dropped or self.aborted

    def in_stage(self, *stages: TicketStage) -> bool:
        """Return True when the child is in one of the given stages."""
        return self.stage in stages

    def move(self, dest: TicketStage, trigger: str) -> None:
        """Apply a legal child transition, including the dependency guard."""
        ctx = ChildGuardContext(dependencies_verified=self.dependencies_verified)
        assert_legal_transition(
            self.stage.value, dest.value, trigger, scope="child", ctx=ctx
        )
        self.stage = dest

    def work_input(self) -> TicketWorkInput:
        """Build the shared implement/review/verify activity input."""
        assert self.input is not None
        return TicketWorkInput(
            ticket_id=self.input.ticket_id,
            execution_id=self.input.execution_id,
            story_id=self.input.story_id,
            config_root=self.input.config_root,
        )

    def result(self) -> TicketResult:
        """Build the child result from current stage and detail."""
        assert self.input is not None
        return TicketResult(
            ticket_id=self.input.ticket_id,
            execution_id=self.input.execution_id,
            stage=self.stage,
            detail=self.detail,
            sandbox=self.sandbox,
            deliverables=self.deliverables,
        )


async def notify_parent(runtime: TicketRuntime, *, unhandled_failure: bool = False) -> None:
    """Signal ticket progress to the parent, or skip when started without a parent."""
    if runtime.input is None or not runtime.input.parent_workflow_id:
        return
    handle = workflow.get_external_workflow_handle(runtime.input.parent_workflow_id)
    failed = unhandled_failure or runtime.stage in {TicketStage.FAILED, TicketStage.BLOCKED}
    await handle.signal(
        "ticket_state_changed",
        TicketStateUpdate(
            ticket_id=runtime.input.ticket_id,
            stage=runtime.stage,
            unhandled_failure=failed,
            detail=runtime.detail,
            sandbox=runtime.sandbox,
            deliverables=runtime.deliverables,
        ),
    )


async def wait_until_eligible(runtime: TicketRuntime) -> bool:
    """Wait until dependencies are verified, or stop if dropped/aborted.

    Returns:
        True when the child may enter Implementing.
    """
    if not runtime.dependencies_verified and not runtime.stopped():
        await workflow.wait_condition(
            lambda: runtime.dependencies_verified or runtime.stopped()
        )
    return runtime.dependencies_verified and not runtime.stopped()


async def wait_for_retry_or_stop(runtime: TicketRuntime) -> None:
    """Park a failed or blocked ticket until retry, drop, or abort."""
    runtime.retry_requested = False
    await workflow.wait_condition(
        lambda: runtime.retry_requested or runtime.stopped()
    )
    runtime.retry_requested = False


async def finish_stopped(runtime: TicketRuntime) -> TicketResult:
    """Move a stopped child to Dropped unless it already finished Verified."""
    if runtime.stage is TicketStage.VERIFIED:
        return runtime.result()
    if runtime.stage is not TicketStage.DROPPED:
        trigger = "abort" if runtime.aborted else "drop_ticket"
        runtime.move(TicketStage.DROPPED, trigger)
        await notify_parent(runtime)
    return runtime.result()


async def run_implement(runtime: TicketRuntime) -> None:
    """Claim the ticket workspace, apply an optional sandbox write, then implement."""
    work = runtime.work_input()
    claim = await workflow.execute_activity(
        claim_ticket_workspace,
        work,
        start_to_close_timeout=CLAIM_TIMEOUT,
        retry_policy=CLAIM_RETRY,
    )
    runtime.sandbox = SandboxRecord(
        authorized_worktree=claim.authorized_worktree,
        status="authorized" if claim.authorized_worktree else "unauthorized",
    )
    if runtime.input is not None and runtime.input.sandbox_write is not None:
        wrote = await workflow.execute_activity(
            apply_sandbox_write,
            SandboxWriteInput(
                ticket_id=work.ticket_id,
                execution_id=work.execution_id,
                story_id=work.story_id,
                config_root=work.config_root,
                path=runtime.input.sandbox_write.path,
                content=runtime.input.sandbox_write.content,
                authorized_worktree=claim.authorized_worktree,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        runtime.sandbox = wrote.record
        if not wrote.allowed:
            runtime.detail = wrote.record.reason or "sandbox write rejected"
            runtime.move(TicketStage.FAILED, "activity_failed")
            await notify_parent(runtime, unhandled_failure=True)
            return
    result = await workflow.execute_activity(
        implement_ticket,
        work,
        start_to_close_timeout=IMPLEMENT_TIMEOUT,
        heartbeat_timeout=IMPLEMENT_HEARTBEAT_TIMEOUT,
        retry_policy=NO_RETRY,
    )
    if runtime.stopped():
        return
    runtime.deliverables = result.deliverables
    complete = result.diff_submitted and is_complete_deliverables(result.deliverables)
    if not complete:
        runtime.detail = result.detail or "交付物不齐，不能进入 Reviewing"
        runtime.move(TicketStage.FAILED, "activity_failed")
        await notify_parent(runtime, unhandled_failure=True)
        return
    runtime.detail = result.detail
    runtime.move(TicketStage.REVIEWING, "diff_submitted")
    await notify_parent(runtime)


async def run_review(runtime: TicketRuntime) -> None:
    """Run the review stub and enter HumanGate or Verifying."""
    findings = await workflow.execute_activity(
        review_ticket,
        runtime.work_input(),
        start_to_close_timeout=ACTIVITY_TIMEOUT,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        retry_policy=NO_RETRY,
    )
    if runtime.stopped():
        return
    if findings.requires_human_gate():
        runtime.move(TicketStage.HUMAN_GATE, "blocking_or_paradigm_drift")
        await notify_parent(runtime)
        return
    runtime.move(TicketStage.VERIFYING, "advisory_only")
    await notify_parent(runtime)


async def run_human_gate(runtime: TicketRuntime) -> None:
    """Wait for review_override and route to rework, verify, or blocked."""
    await workflow.wait_condition(lambda: runtime.override is not None or runtime.stopped())
    if runtime.stopped():
        return
    decision = runtime.override.decision if runtime.override is not None else "rework"
    runtime.override = None
    if decision == "reject":
        runtime.detail = "review_override reject"
        runtime.move(TicketStage.BLOCKED, "review_override_reject")
        await notify_parent(runtime, unhandled_failure=True)
        return
    if decision == "rework":
        runtime.move(TicketStage.IMPLEMENTING, "review_override_rework")
        await notify_parent(runtime)
        return
    runtime.move(TicketStage.VERIFYING, "review_override_approve")
    await notify_parent(runtime)


async def run_verify(runtime: TicketRuntime) -> bool:
    """Run the verify stub. Return True when the ticket passed."""
    result: VerifyResult = await workflow.execute_activity(
        verify_ticket,
        runtime.work_input(),
        start_to_close_timeout=ACTIVITY_TIMEOUT,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        retry_policy=NO_RETRY,
    )
    if runtime.stopped():
        return False
    runtime.detail = result.evidence
    return result.passed


async def run_one_cycle(runtime: TicketRuntime) -> None:
    """Advance one child-loop iteration, or wait for retry/drop/abort."""
    try:
        await _run_one_cycle_body(runtime)
    except ActivityError as exc:
        if is_cancelled_exception(exc) or runtime.aborted:
            runtime.aborted = True
            return
        runtime.detail = str(exc)
        if runtime.stage not in {TicketStage.FAILED, TicketStage.BLOCKED}:
            runtime.move(TicketStage.FAILED, "activity_failed")
        await notify_parent(runtime, unhandled_failure=True)


async def _run_one_cycle_body(runtime: TicketRuntime) -> None:
    if runtime.in_stage(TicketStage.FAILED, TicketStage.BLOCKED):
        await wait_for_retry_or_stop(runtime)
        if runtime.stopped():
            return
        runtime.move(TicketStage.IMPLEMENTING, "retry_ticket")
        await notify_parent(runtime)
        return
    if runtime.in_stage(TicketStage.IMPLEMENTING):
        await run_implement(runtime)
        return
    if runtime.in_stage(TicketStage.REVIEWING):
        await run_review(runtime)
        return
    if runtime.in_stage(TicketStage.HUMAN_GATE):
        await run_human_gate(runtime)
        return
    if runtime.in_stage(TicketStage.VERIFYING):
        if await run_verify(runtime):
            runtime.move(TicketStage.VERIFIED, "verify_passed")
            await notify_parent(runtime)
            return
        if runtime.stopped():
            return
        runtime.move(TicketStage.IMPLEMENTING, "verify_failed")
        await notify_parent(runtime)
