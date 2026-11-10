"""DevPipelineWorkflow: one parent execution equals one Story."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ParentClosePolicy

from dev_pipeline.models import (
    PipelineResult,
    PipelineSnapshot,
    PromoteRuleRequest,
    PublishRequest,
    PublishResult,
    RetroInput,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    SpikeInput,
    SpikeResult,
    StoryInput,
    TicketInput,
    TicketSnapshot,
    TicketSpec,
    TicketStateUpdate,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.topology import eligible_ticket_ids
from dev_pipeline.transitions import (
    ParentGuardContext,
    assert_legal_transition,
    can_enter_test_deploying,
    required_unhandled_failures,
)
from dev_pipeline.workflows.ticket import TicketWorkflow

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.config import validate_config_root
    from dev_pipeline.activities.stubs import publish_to_test_env, run_spike, write_retro_record

_ACTIVITY_TIMEOUT = timedelta(seconds=60)
_NO_RETRY = RetryPolicy(maximum_attempts=1)

_REOPEN_MESSAGE = (
    "reopen 必须使用新的 execution_id 启动新的父执行，不能在已损坏的本次执行上恢复。"
)
_PROMOTE_AUTO_REJECTED = "promote_rule 自动晋升已拒绝；全局规则不得静默升级。"
_PROMOTE_RECORDED = "promote_rule 已记录，本骨架不执行晋升（拒绝自动生效）。"


@workflow.defn
class DevPipelineWorkflow:
    """Parent legal path, human gates, child orchestration, and TestDeploying."""

    def __init__(self) -> None:
        self._stage = ParentStage.INTAKE
        self._waiting: str | None = "lock_scope"
        self._input: StoryInput | None = None
        self._scope: ScopeLock | None = None
        self._scheme: SchemeDecision | None = None
        self._spike_accepted: bool | None = None
        self._abort_requested = False
        self._abort_reason = "abort"
        self._blocked_reason: str | None = None
        self._last_illegal: str | None = None
        self._promote_status: str | None = None
        self._test_deploy_status = TestDeployStatus.NOT_STARTED
        self._test_deploy_address: str | None = None
        self._tickets: dict[str, TicketSnapshot] = {}
        self._specs: list[TicketSpec] = []
        self._ticket_event = False

    @workflow.run
    async def run(self, payload: StoryInput) -> PipelineResult:
        self._input = payload
        self._install_tickets(payload.tickets)
        await workflow.execute_activity(
            validate_config_root,
            payload.config_root,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_NO_RETRY,
        )

        await self._wait_for_scope()
        if self._abort_requested:
            return self._block("abort")

        await self._wait_for_scheme()
        if self._abort_requested:
            return self._block("abort")

        if self._scheme is not None and self._scheme.has_high_risk_assumption:
            blocked = await self._run_spike()
            if blocked is not None:
                return blocked

        self._move(ParentStage.TICKETS_READY, "no_high_risk_assumption")
        self._waiting = None
        if self._abort_requested:
            return self._block("abort")

        self._move(ParentStage.IMPLEMENTING, "start_eligible_children")
        await self._orchestrate_tickets()
        if self._abort_requested:
            return self._block(self._abort_reason)

        return await self._run_test_deploy_and_retro()

    async def _wait_for_scope(self) -> None:
        self._stage = ParentStage.INTAKE
        self._waiting = "lock_scope"
        await workflow.wait_condition(lambda: self._scope is not None or self._abort_requested)
        if self._abort_requested:
            return
        self._move(ParentStage.SCOPE_LOCKED, "lock_scope")
        self._waiting = "decide_scheme"

    async def _wait_for_scheme(self) -> None:
        await workflow.wait_condition(lambda: self._scheme is not None or self._abort_requested)
        if self._abort_requested:
            return
        self._move(ParentStage.SCHEME_DECIDED, "decide_scheme")
        self._waiting = None

    async def _run_spike(self) -> PipelineResult | None:
        assert self._input is not None
        self._move(ParentStage.SPIKE, "high_risk_assumption")
        self._waiting = "approve_spike"
        try:
            spike: SpikeResult = await workflow.execute_activity(
                run_spike,
                SpikeInput(story_id=self._input.story_id, execution_id=self._input.execution_id),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_NO_RETRY,
            )
        except ActivityError as exc:
            return self._block(f"red-line fail: {exc}")
        if spike.red_line_failed:
            self._move(ParentStage.BLOCKED, "red_line_fail")
            return self._finish_blocked("击穿红线且无防护")
        await workflow.wait_condition(
            lambda: self._spike_accepted is not None or self._abort_requested
        )
        if self._abort_requested:
            return self._block(self._abort_reason)
        if self._spike_accepted is False:
            self._move(ParentStage.BLOCKED, "approve_spike_reject")
            return self._finish_blocked("approve_spike 拒绝")
        self._move(ParentStage.TICKETS_READY, "approve_spike_accept")
        self._waiting = None
        return None

    async def _orchestrate_tickets(self) -> None:
        while not self._abort_requested:
            failures = required_unhandled_failures(self._ticket_list())
            if failures and not self._has_in_flight_required():
                self._waiting = "retry_ticket_or_drop_ticket_or_abort"
            else:
                self._waiting = None
            if can_enter_test_deploying(self._ticket_list()):
                return
            started_any = await self._start_eligible()
            if can_enter_test_deploying(self._ticket_list()):
                return
            if not started_any:
                self._ticket_event = False
                await workflow.wait_condition(
                    lambda: self._ticket_event or self._abort_requested
                )
                self._ticket_event = False

    async def _start_eligible(self) -> bool:
        assert self._input is not None
        started = False
        for ticket_id in eligible_ticket_ids(self._specs, self._ticket_list()):
            snap = self._tickets[ticket_id]
            spec = next(item for item in self._specs if item.ticket_id == ticket_id)
            await workflow.start_child_workflow(
                TicketWorkflow.run,
                TicketInput(
                    ticket_id=ticket_id,
                    execution_id=self._input.execution_id,
                    story_id=self._input.story_id,
                    config_root=self._input.config_root,
                    parent_workflow_id=workflow.info().workflow_id,
                    depends_on=list(spec.depends_on),
                ),
                id=ticket_workflow_id(ticket_id, self._input.execution_id),
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
            snap.started = True
            snap.stage = TicketStage.IMPLEMENTING
            started = True
        return started

    async def _run_test_deploy_and_retro(self) -> PipelineResult:
        assert self._input is not None
        ctx = self._guard_ctx()
        assert_legal_transition(
            self._stage.value,
            ParentStage.TEST_DEPLOYING.value,
            "all_required_tickets_verified",
            scope="parent",
            ctx=ctx,
        )
        self._stage = ParentStage.TEST_DEPLOYING
        self._waiting = None
        self._test_deploy_status = TestDeployStatus.IN_PROGRESS
        request = PublishRequest(
            story_id=self._input.story_id,
            execution_id=self._input.execution_id,
            target="test",
        )
        try:
            published: PublishResult = await workflow.execute_activity(
                publish_to_test_env,
                request,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_NO_RETRY,
            )
        except ActivityError as exc:
            self._test_deploy_status = TestDeployStatus.FAILED
            self._move(ParentStage.BLOCKED, "test_deploy_failure")
            return self._finish_blocked(f"测试部署失败，不得进入 Retro: {exc}")
        if published.status != "succeeded":
            self._test_deploy_status = TestDeployStatus.FAILED
            self._move(ParentStage.BLOCKED, "test_deploy_failure")
            return self._finish_blocked("测试部署失败，不得进入 Retro")
        self._test_deploy_status = TestDeployStatus.SUCCEEDED
        self._test_deploy_address = published.address
        self._move(ParentStage.RETRO, "test_deploy_success", ctx=self._guard_ctx())
        await workflow.execute_activity(
            write_retro_record,
            RetroInput(story_id=self._input.story_id, execution_id=self._input.execution_id),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_NO_RETRY,
        )
        if self._abort_requested:
            return self._block(self._abort_reason)
        self._move(ParentStage.DONE, "retro_written")
        return self._result()

    def _move(
        self,
        dest: ParentStage,
        trigger: str,
        *,
        ctx: ParentGuardContext | None = None,
    ) -> None:
        # TicketsReady may already have been entered via approve_spike_accept.
        if self._stage is dest:
            return
        assert_legal_transition(
            self._stage.value,
            dest.value,
            trigger,
            scope="parent",
            ctx=ctx or self._guard_ctx(),
        )
        self._stage = dest

    def _block(self, reason: str) -> PipelineResult:
        if self._stage is not ParentStage.BLOCKED:
            self._move(ParentStage.BLOCKED, "abort")
        return self._finish_blocked(reason)

    def _finish_blocked(self, reason: str) -> PipelineResult:
        self._stage = ParentStage.BLOCKED
        self._waiting = None
        self._blocked_reason = reason
        return self._result()

    def _result(self) -> PipelineResult:
        assert self._input is not None
        return PipelineResult(
            story_id=self._input.story_id,
            execution_id=self._input.execution_id,
            stage=self._stage,
            test_deploy_status=self._test_deploy_status,
            blocked_reason=self._blocked_reason,
            test_deploy_address=self._test_deploy_address,
        )

    def _install_tickets(self, specs: list[TicketSpec]) -> None:
        if not specs:
            return
        self._specs = list(specs)
        self._tickets = {
            spec.ticket_id: TicketSnapshot(
                ticket_id=spec.ticket_id,
                stage=TicketStage.PENDING,
                required=spec.required,
                depends_on=list(spec.depends_on),
                started=False,
            )
            for spec in specs
        }
        self._ticket_event = True

    def _ticket_list(self) -> list[TicketSnapshot]:
        return [
            self._tickets[spec.ticket_id]
            for spec in self._specs
            if spec.ticket_id in self._tickets
        ]

    def _guard_ctx(self) -> ParentGuardContext:
        return ParentGuardContext(
            tickets=tuple(self._ticket_list()),
            test_deploy_status=self._test_deploy_status,
        )

    def _has_in_flight_required(self) -> bool:
        return any(
            item.required and item.started and not item.unhandled_failure
            and item.stage
            in {
                TicketStage.IMPLEMENTING,
                TicketStage.REVIEWING,
                TicketStage.HUMAN_GATE,
                TicketStage.VERIFYING,
            }
            for item in self._ticket_list()
        )

    def _record_illegal(self, exc: Exception) -> None:
        self._last_illegal = str(exc)

    @workflow.signal
    def lock_scope(self, payload: ScopeLock) -> None:
        # Signals in the same task run before run() can leave Intake, so we
        # only reject once the workflow has already moved past this gate.
        if self._scope is not None or self._stage not in {ParentStage.INTAKE}:
            self._last_illegal = (
                f"非法转移: {self._stage.value} --lock_scope--> ScopeLocked。当前阶段不能锁范围"
            )
            return
        self._scope = payload

    @workflow.signal
    def decide_scheme(self, payload: SchemeDecision) -> None:
        if self._scheme is not None or self._stage not in {
            ParentStage.INTAKE,
            ParentStage.SCOPE_LOCKED,
        }:
            self._last_illegal = (
                f"非法转移: {self._stage.value} --decide_scheme--> SchemeDecided。"
                "当前阶段不能拍方案"
            )
            return
        self._scheme = payload
        if payload.tickets:
            self._install_tickets(payload.tickets)

    @workflow.signal
    def approve_spike(self, accepted: bool) -> None:
        if self._spike_accepted is not None or self._stage not in {
            ParentStage.INTAKE,
            ParentStage.SCOPE_LOCKED,
            ParentStage.SCHEME_DECIDED,
            ParentStage.SPIKE,
        }:
            self._last_illegal = (
                f"非法转移: {self._stage.value} --approve_spike--> TicketsReady。当前不在 Spike"
            )
            return
        self._spike_accepted = accepted

    @workflow.signal
    def abort(self, reason: str = "abort") -> None:
        if self._stage is ParentStage.DONE:
            self._last_illegal = "已完成的执行不能 abort"
            return
        self._abort_requested = True
        self._abort_reason = reason or "abort"
        self._ticket_event = True

    @workflow.signal
    def reopen(self, new_execution_id: str = "") -> None:
        # reopen never resumes this execution, even if a new id is supplied.
        _ = new_execution_id
        self._last_illegal = _REOPEN_MESSAGE

    @workflow.signal
    def promote_rule(self, payload: PromoteRuleRequest) -> None:
        if payload.automatic:
            self._promote_status = _PROMOTE_AUTO_REJECTED
            self._last_illegal = _PROMOTE_AUTO_REJECTED
            return
        self._promote_status = _PROMOTE_RECORDED

    @workflow.signal
    async def review_override(self, payload: ReviewOverride) -> None:
        await self._route_to_child(payload.ticket_id, "review_override", payload)

    @workflow.signal
    async def retry_ticket(self, ticket_id: str) -> None:
        snap = self._tickets.get(ticket_id)
        if snap is not None:
            snap.unhandled_failure = False
            self._ticket_event = True
        await self._route_to_child(ticket_id, "retry_ticket")

    @workflow.signal
    async def drop_ticket(self, ticket_id: str) -> None:
        snap = self._tickets.get(ticket_id)
        if snap is not None:
            snap.required = False
            snap.unhandled_failure = False
            snap.stage = TicketStage.DROPPED
            self._ticket_event = True
        await self._route_to_child(ticket_id, "drop_ticket")

    @workflow.signal
    def ticket_state_changed(self, update: TicketStateUpdate) -> None:
        snap = self._tickets.get(update.ticket_id)
        if snap is None:
            return
        snap.stage = update.stage
        snap.detail = update.detail
        snap.started = True
        if update.stage is TicketStage.DROPPED:
            snap.required = False
            snap.unhandled_failure = False
        else:
            snap.unhandled_failure = update.unhandled_failure
        self._ticket_event = True

    async def _route_to_child(
        self, ticket_id: str, signal_name: str, payload: object | None = None
    ) -> None:
        if self._input is None:
            return
        snap = self._tickets.get(ticket_id)
        if snap is None or not snap.started:
            return
        child_id = ticket_workflow_id(ticket_id, self._input.execution_id)
        handle = workflow.get_external_workflow_handle(child_id)
        if payload is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, payload)

    @workflow.query
    def snapshot(self) -> PipelineSnapshot:
        story_id = self._input.story_id if self._input is not None else ""
        execution_id = self._input.execution_id if self._input is not None else ""
        return PipelineSnapshot(
            story_id=story_id,
            execution_id=execution_id,
            stage=self._stage,
            waiting_signal=self._waiting,
            tickets=self._ticket_list(),
            test_deploy_status=self._test_deploy_status,
            blocked_reason=self._blocked_reason,
            last_illegal=self._last_illegal,
            promote_rule_status=self._promote_status,
            test_deploy_address=self._test_deploy_address,
        )
