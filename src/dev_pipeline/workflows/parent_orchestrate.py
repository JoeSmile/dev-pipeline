"""Parent orchestration steps mixed into DevPipelineWorkflow."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TemporalError
from temporalio.workflow import ChildWorkflowCancellationType, ParentClosePolicy

from dev_pipeline.constants import TEST_PUBLISH_TARGET, WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import (
    PipelineResult,
    PublishRequest,
    PublishResult,
    RetroInput,
    SpikeInput,
    SpikeResult,
    StoryInput,
    TicketInput,
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
    from dev_pipeline.activities.stubs import publish_to_test_env, run_spike, write_retro_record

ACTIVITY_TIMEOUT = timedelta(seconds=60)
NO_RETRY = RetryPolicy(maximum_attempts=1)


class ParentOrchestrationMixin:
    """Scope, spike, child start/stop, and test-deploy steps for the parent."""

    _state: ParentState
    _children: dict[str, TicketChildHandle]
    _finish_terminal: Callable[[PipelineResult], Awaitable[PipelineResult]]
    _install_budget: Callable[[], Awaitable[None]]

    def _move(self, dest: ParentStage, trigger: str) -> None:
        self._state.move(dest, trigger)

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

    async def _wait_for_scope(self) -> None:
        state = self._state
        state.stage = ParentStage.INTAKE
        state.waiting = "lock_scope"
        await workflow.wait_condition(lambda: state.scope is not None or state.abort_requested)
        if state.abort_requested:
            return
        self._move(ParentStage.SCOPE_LOCKED, "lock_scope")
        state.waiting = "decide_scheme"

    async def _wait_for_scheme(self) -> None:
        state = self._state
        await workflow.wait_condition(lambda: state.scheme is not None or state.abort_requested)
        if state.abort_requested:
            return
        self._move(ParentStage.SCHEME_DECIDED, "decide_scheme")
        state.waiting = None

    async def _run_spike(self) -> PipelineResult | None:
        state = self._state
        assert state.input is not None
        self._move(ParentStage.SPIKE, "high_risk_assumption")
        state.waiting = "approve_spike"
        try:
            spike: SpikeResult = await workflow.execute_activity(
                run_spike,
                SpikeInput(story_id=state.input.story_id, execution_id=state.input.execution_id),
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except ActivityError as exc:
            return await self._block(f"red-line fail: {exc}")
        if spike.red_line_failed:
            self._move(ParentStage.BLOCKED, "red_line_fail")
            return await self._finish_terminal(state.finish_blocked("击穿红线且无防护"))
        await workflow.wait_condition(
            lambda: state.spike_accepted is not None or state.abort_requested
        )
        if state.abort_requested:
            return await self._block(state.abort_reason)
        if state.spike_accepted is False:
            self._move(ParentStage.BLOCKED, "approve_spike_reject")
            return await self._finish_terminal(state.finish_blocked("approve_spike 拒绝"))
        self._move(ParentStage.TICKETS_READY, "approve_spike_accept")
        state.waiting = None
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
        started = False
        for ticket_id in eligible_ticket_ids(self._state.specs, self._state.ticket_list()):
            await self._start_child(ticket_id)
            started = True
        return started

    async def _start_child(self, ticket_id: str) -> None:
        existing = self._children.get(ticket_id)
        if existing is not None:
            return
        payload = self._state.input
        assert payload is not None
        spec = next(item for item in self._state.specs if item.ticket_id == ticket_id)
        handle, started = await start_or_attach_ticket_child(
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
        self._state.tickets[ticket_id].started = True
        if started:
            await handle.signal(TicketWorkflow.dependencies_satisfied)

    async def _run_test_deploy_and_retro(self) -> PipelineResult:
        state = self._state
        assert state.input is not None
        self._move(ParentStage.TEST_DEPLOYING, "authorize_test_deploy")
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
        self._move(ParentStage.RETRO, "test_deploy_success")
        await workflow.execute_activity(
            write_retro_record,
            RetroInput(story_id=state.input.story_id, execution_id=state.input.execution_id),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        if state.abort_requested:
            return await self._block(state.abort_reason)
        self._move(ParentStage.DONE, "retro_written")
        return await self._finish_terminal(state.result())

    async def _fail_test_deploy(self, reason: str) -> PipelineResult:
        self._state.test_deploy_status = TestDeployStatus.FAILED
        self._move(ParentStage.BLOCKED, "test_deploy_failure")
        return await self._finish_terminal(self._state.finish_blocked(reason))

    async def _block(self, reason: str) -> PipelineResult:
        await self._stop_children()
        if self._state.stage is not ParentStage.BLOCKED:
            self._move(ParentStage.BLOCKED, "abort")
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
