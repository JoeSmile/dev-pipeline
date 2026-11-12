"""Parent orchestration steps mixed into DevPipelineWorkflow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TemporalError
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

from dev_pipeline.constants import TEST_PUBLISH_TARGET, WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import (
    SPIKE_BROKEN,
    SPIKE_OVERRUN,
    SPIKE_VERIFIED,
    PipelineResult,
    PublishRequest,
    PublishResult,
    RetroInput,
    ScopeLock,
    SpikeInput,
    SpikeRecord,
    SpikeResult,
    StoryDraftInput,
    StoryInput,
    TicketInput,
    format_scope_lock_time,
    is_complete_scope_lock,
    red_line_statements,
)
from dev_pipeline.stages import ParentStage, TestDeployStatus
from dev_pipeline.topology import eligible_ticket_ids
from dev_pipeline.transitions import can_enter_test_deploying, unhandled_failures
from dev_pipeline.workflows.child_start import (
    CHILD_EXECUTION_TIMEOUT,
    CHILD_RETRY,
    TicketChildHandle,
    cancel_ticket_handle,
    handle_is_done,
    start_or_attach_ticket_child,
)
from dev_pipeline.workflows.parent_state import ParentState
from dev_pipeline.workflows.ticket import TicketWorkflow

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.config import validate_config_root
    from dev_pipeline.activities.spike import load_spike_config, run_spike
    from dev_pipeline.activities.story_draft import normalize_story_draft
    from dev_pipeline.activities.stubs import publish_to_test_env, write_retro_record

ACTIVITY_TIMEOUT = timedelta(seconds=60)
NO_RETRY = RetryPolicy(maximum_attempts=1)


class ParentOrchestrationMixin:
    """Scope, spike, child start/stop, and test-deploy steps for the parent."""

    _state: ParentState
    _children: dict[str, TicketChildHandle]
    _finish_terminal: Callable[[PipelineResult], Awaitable[PipelineResult]]
    _install_budget: Callable[[], Awaitable[None]]

    async def _move(self, dest: ParentStage, trigger: str) -> None:
        previous = self._state.stage
        self._state.move(dest, trigger)
        if self._state.stage is not previous:
            await self._after_stage_change()

    async def _after_stage_change(self) -> None:
        """Hook for mixins that must run after a real parent stage change."""
        return

    async def _bootstrap(self, payload: StoryInput) -> None:
        self._state.input = payload
        self._state.install_tickets(payload.tickets)
        await workflow.execute_activity(
            validate_config_root,
            payload.config_root,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        await self._install_budget()
        await self._after_stage_change()

    async def _wait_for_scope(self) -> None:
        state = self._state
        state.stage = ParentStage.INTAKE
        assert state.input is not None
        state.story_draft = await workflow.execute_activity(
            normalize_story_draft,
            StoryDraftInput(
                intake=state.input.intake,
                summary=state.input.summary,
                config_root=state.input.config_root,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        inherited = self._inherited_scope()
        if inherited is not None:
            state.scope = inherited
            await self._move(ParentStage.SCOPE_LOCKED, "lock_scope")
            state.waiting = "decide_scheme"
            return
        state.waiting = "lock_scope"
        await workflow.wait_condition(lambda: state.scope is not None or state.abort_requested)
        if state.abort_requested:
            return
        await self._move(ParentStage.SCOPE_LOCKED, "lock_scope")
        state.waiting = "decide_scheme"

    def _inherited_scope(self) -> ScopeLock | None:
        """Return a complete inherited lock, or None when reopen has nothing to copy."""
        payload = self._state.input
        if payload is None or payload.inherited_scope is None:
            return None
        inherited = payload.inherited_scope
        if not is_complete_scope_lock(inherited):
            return None
        return ScopeLock(
            in_scope=inherited.in_scope,
            out_of_scope=inherited.out_of_scope,
            locked_by=inherited.locked_by,
            locked_at=inherited.locked_at,
        )

    async def _wait_for_scheme(self) -> None:
        state = self._state
        await workflow.wait_condition(lambda: state.scheme is not None or state.abort_requested)
        if state.abort_requested:
            return
        await self._move(ParentStage.SCHEME_DECIDED, "decide_scheme")
        state.waiting = None

    async def _enter_optional_spike(self) -> PipelineResult | None:
        """Run the optional Spike and apply the falsifiable-conclusion gate."""
        state = self._state
        await self._move(ParentStage.SPIKE, "high_risk_assumption")
        state.waiting = None
        blocked = await self._run_spike_and_record()
        if blocked is not None or state.abort_requested:
            return blocked
        assert state.spike is not None
        if state.spike.conclusion == SPIKE_VERIFIED:
            await self._enter_tickets_ready("spike_verified")  # type: ignore[attr-defined]
            return None
        if state.spike.conclusion in {SPIKE_BROKEN, SPIKE_OVERRUN} and not (
            state.spike.has_protection
        ):
            trigger = "spike_overrun" if state.spike.timed_out else "red_line_fail"
            reason = (
                "Spike 超时未验证，按无防护处理"
                if state.spike.timed_out
                else "Spike 击穿红线且无防护"
            )
            state.waiting = None
            state.blocked_reason = reason
            await self._move(ParentStage.BLOCKED, trigger)
            return await self._finish_terminal(state.finish_blocked(reason))
        state.waiting = "approve_spike"
        await workflow.wait_condition(
            lambda: state.spike_accepted is not None or state.abort_requested
        )
        if state.abort_requested:
            return await self._block(state.abort_reason)
        if state.spike_accepted is False:
            reason = "approve_spike 拒绝"
            state.waiting = None
            state.blocked_reason = reason
            await self._move(ParentStage.BLOCKED, "approve_spike_reject")
            return await self._finish_terminal(state.finish_blocked(reason))
        await self._enter_tickets_ready("approve_spike_accept")  # type: ignore[attr-defined]
        return None

    async def _run_spike_and_record(self) -> PipelineResult | None:
        """Execute the spike activity within the configured duration bound."""
        state = self._state
        payload = state.input
        assert payload is not None
        scheme = state.scheme
        assert scheme is not None
        statement = red_line_statements(list(scheme.hypotheses))
        config = await workflow.execute_activity(
            load_spike_config,
            payload.config_root,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        budget = timedelta(seconds=config.timeout_seconds)
        started_at = format_scope_lock_time(workflow.now())
        spike_input = SpikeInput(
            story_id=payload.story_id,
            execution_id=payload.execution_id,
            config_root=payload.config_root,
            statement=statement,
            budget_seconds=config.timeout_seconds,
        )
        spike_task: asyncio.Task[SpikeResult] = asyncio.create_task(
            workflow.execute_activity(
                run_spike,
                spike_input,
                start_to_close_timeout=budget,
                retry_policy=NO_RETRY,
            )
        )
        abort_task = asyncio.create_task(workflow.wait_condition(lambda: state.abort_requested))
        try:
            await workflow.wait(
                [spike_task, abort_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if state.abort_requested:
                if not spike_task.done():
                    spike_task.cancel()
                    try:
                        await spike_task
                    except (asyncio.CancelledError, ActivityError):
                        pass
                return await self._block(state.abort_reason)
            try:
                result = await spike_task
            except ActivityError:
                state.spike = SpikeRecord(
                    conclusion=SPIKE_OVERRUN,
                    statement=statement,
                    detail="Spike 超过可证伪时限，结论记为超时未验证，按无防护处理。",
                    has_protection=False,
                    timed_out=True,
                    budget_seconds=config.timeout_seconds,
                    started_at=started_at,
                    finished_at=format_scope_lock_time(workflow.now()),
                    backend="桩",
                    source=config.source,
                )
                return None
        finally:
            if not abort_task.done():
                abort_task.cancel()
                try:
                    await abort_task
                except (asyncio.CancelledError, ActivityError):
                    pass
        broken = result.red_line_failed or result.conclusion == SPIKE_BROKEN
        conclusion = SPIKE_BROKEN if broken else SPIKE_VERIFIED
        state.spike = SpikeRecord(
            conclusion=conclusion,
            statement=result.statement.strip() or statement,
            detail=result.detail,
            has_protection=bool(result.has_protection) if conclusion == SPIKE_BROKEN else False,
            timed_out=False,
            budget_seconds=config.timeout_seconds,
            started_at=started_at,
            finished_at=format_scope_lock_time(workflow.now()),
            backend=result.backend or "桩",
            source=config.source,
        )
        return None

    def _refresh_ticket_wait(self) -> None:
        state = self._state
        if can_enter_test_deploying(state.ticket_list()) and not state.test_deploy_authorized:
            state.waiting = WAITING_AUTHORIZE_TEST_DEPLOY
            return
        if unhandled_failures(state.ticket_list()) and not state.has_in_flight():
            state.waiting = "retry_ticket_or_drop_ticket_or_abort"
            return
        state.waiting = None

    async def _orchestrate_tickets(self) -> None:
        state = self._state
        while not state.abort_requested:
            self._refresh_ticket_wait()
            if can_enter_test_deploying(state.ticket_list()) and state.test_deploy_authorized:
                return
            started_any = await self._start_eligible()
            if can_enter_test_deploying(state.ticket_list()) and state.test_deploy_authorized:
                return
            if started_any:
                continue
            state.ticket_event = False
            await workflow.wait_condition(lambda: state.ticket_event or state.abort_requested)
            state.ticket_event = False

    async def _start_eligible(self) -> bool:
        """Signal dependencies_satisfied for tickets whose deps are now verified."""
        started = False
        for ticket_id in eligible_ticket_ids(self._state.specs, self._state.ticket_list()):
            handle = self._children.get(ticket_id)
            if handle is None:
                await self._create_child(ticket_id)
                handle = self._children[ticket_id]
            snap = self._state.tickets[ticket_id]
            if snap.started:
                continue
            await handle.signal(TicketWorkflow.dependencies_satisfied)
            snap.started = True
            started = True
        return started

    async def _create_all_ticket_children(self) -> None:
        """Create one Child for every required ticket once the list is complete."""
        for spec in self._state.specs:
            await self._create_child(spec.ticket_id)

    async def _create_child(self, ticket_id: str) -> None:
        existing = self._children.get(ticket_id)
        if existing is not None:
            snap = self._state.tickets.get(ticket_id)
            if snap is not None:
                snap.child_created = True
            return
        payload = self._state.input
        assert payload is not None
        spec = next(item for item in self._state.specs if item.ticket_id == ticket_id)
        handle, _started = await start_or_attach_ticket_child(
            TicketInput(
                ticket_id=ticket_id,
                execution_id=payload.execution_id,
                story_id=payload.story_id,
                config_root=payload.config_root,
                parent_workflow_id=workflow.info().workflow_id,
                depends_on=list(spec.depends_on),
            ),
            parent_close_policy=ParentClosePolicy.TERMINATE,
            cancellation_type=ChildWorkflowCancellationType.TRY_CANCEL,
            execution_timeout=CHILD_EXECUTION_TIMEOUT,
            retry_policy=CHILD_RETRY,
        )
        self._children[ticket_id] = handle
        self._state.tickets[ticket_id].child_created = True

    async def _run_test_deploy_and_retro(self) -> PipelineResult:
        state = self._state
        assert state.input is not None
        await self._move(ParentStage.TEST_DEPLOYING, "authorize_test_deploy")
        state.waiting = None
        state.test_deploy_status = TestDeployStatus.IN_PROGRESS
        request = PublishRequest(
            story_id=state.input.story_id,
            execution_id=state.input.execution_id,
            target=TEST_PUBLISH_TARGET,
        )
        try:
            published: PublishResult = await workflow.execute_activity(
                publish_to_test_env,
                request,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except ActivityError as exc:
            return await self._fail_test_deploy(f"测试部署失败，不得进入 Retro: {exc}")
        if published.status != "succeeded":
            return await self._fail_test_deploy("测试部署失败，不得进入 Retro")
        state.test_deploy_status = TestDeployStatus.SUCCEEDED
        state.test_deploy_address = published.address
        await self._move(ParentStage.RETRO, "test_deploy_success")
        await workflow.execute_activity(
            write_retro_record,
            RetroInput(story_id=state.input.story_id, execution_id=state.input.execution_id),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        if state.abort_requested:
            return await self._block(state.abort_reason)
        await self._move(ParentStage.DONE, "retro_written")
        return await self._finish_terminal(state.result())

    async def _fail_test_deploy(self, reason: str) -> PipelineResult:
        self._state.test_deploy_status = TestDeployStatus.FAILED
        await self._move(ParentStage.BLOCKED, "test_deploy_failure")
        return await self._finish_terminal(self._state.finish_blocked(reason))

    async def _block(self, reason: str) -> PipelineResult:
        await self._stop_children()
        if self._state.stage is not ParentStage.BLOCKED:
            await self._move(ParentStage.BLOCKED, "abort")
        return await self._finish_terminal(self._state.finish_blocked(reason))

    async def _stop_children(self) -> None:
        """Signal abort and cancel every running child before the parent blocks."""
        for handle in list(self._children.values()):
            if handle_is_done(handle):
                continue
            try:
                await handle.signal(TicketWorkflow.abort)
            except TemporalError:
                pass
            try:
                await cancel_ticket_handle(handle)
            except TemporalError:
                pass
