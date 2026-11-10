"""Human and child-routed signals mixed into DevPipelineWorkflow."""

from __future__ import annotations

from typing import Any, cast

from temporalio import workflow
from temporalio.workflow import ChildWorkflowHandle

from dev_pipeline.models import (
    PromoteRuleRequest,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    TicketResult,
    TicketStateUpdate,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.transitions import can_enter_test_deploying
from dev_pipeline.workflows.parent_state import ParentState

_REOPEN_MESSAGE = (
    "reopen 必须使用新的 execution_id 启动新的父执行，不能在已损坏的本次执行上恢复。"
)
_PROMOTE_AUTO_REJECTED = "promote_rule 自动晋升已拒绝；全局规则不得静默升级。"
_PROMOTE_RECORDED = "promote_rule 已记录，本骨架不执行晋升（拒绝自动生效）。"


class ParentSignalMixin:
    """Signal handlers for the parent workflow. Mixed into DevPipelineWorkflow."""

    _state: ParentState
    _children: dict[str, ChildWorkflowHandle[Any, TicketResult]]

    @workflow.signal
    def lock_scope(self, payload: ScopeLock) -> None:
        """Lock Story scope. Ignored if the parent has already left Intake."""
        if self._state.scope is not None or self._state.stage is not ParentStage.INTAKE:
            self._state.record_illegal(
                f"非法转移: {self._state.stage.value} --lock_scope--> ScopeLocked。"
                "当前阶段不能锁范围"
            )
            return
        self._state.scope = payload

    @workflow.signal
    def decide_scheme(self, payload: SchemeDecision) -> None:
        """Record the scheme decision. May also install the ticket list."""
        if self._state.scheme is not None or self._state.stage not in {
            ParentStage.INTAKE,
            ParentStage.SCOPE_LOCKED,
        }:
            self._state.record_illegal(
                f"非法转移: {self._state.stage.value} --decide_scheme--> SchemeDecided。"
                "当前阶段不能拍方案"
            )
            return
        self._state.scheme = payload
        if payload.tickets:
            self._state.install_tickets(payload.tickets)

    @workflow.signal
    def approve_spike(self, accepted: bool) -> None:
        """Accept or reject a high-risk spike. Ignored outside the Spike path."""
        if self._state.spike_accepted is not None or self._state.stage not in {
            ParentStage.INTAKE,
            ParentStage.SCOPE_LOCKED,
            ParentStage.SCHEME_DECIDED,
            ParentStage.SPIKE,
        }:
            self._state.record_illegal(
                f"非法转移: {self._state.stage.value} --approve_spike--> TicketsReady。"
                "当前不在 Spike"
            )
            return
        self._state.spike_accepted = accepted

    @workflow.signal
    def abort(self, reason: str = "abort") -> None:
        """Request that this Story stop; running children are cancelled by run()."""
        if self._state.stage is ParentStage.DONE:
            self._state.record_illegal("已完成的执行不能 abort")
            return
        self._state.abort_requested = True
        self._state.abort_reason = reason or "abort"
        self._state.ticket_event = True

    @workflow.signal
    def reopen(self, new_execution_id: str = "") -> None:
        """Reject resume of this execution. A new execution_id must be started."""
        _ = new_execution_id
        self._state.record_illegal(_REOPEN_MESSAGE)

    @workflow.signal
    def promote_rule(self, payload: PromoteRuleRequest) -> None:
        """Record a promote_rule request. Automatic promotion is always rejected."""
        if payload.automatic:
            self._state.promote_status = _PROMOTE_AUTO_REJECTED
            self._state.record_illegal(_PROMOTE_AUTO_REJECTED)
            return
        self._state.promote_status = _PROMOTE_RECORDED

    @workflow.signal
    def authorize_test_deploy(self) -> None:
        """Authorize test-environment publish after all non-dropped tickets verify."""
        if not can_enter_test_deploying(self._state.ticket_list()):
            self._state.record_illegal(
                "尚有 Ticket 未验证过线、仍在飞行阶段，或失败尚未处理，不能授权测试发布"
            )
            return
        if self._state.stage is not ParentStage.IMPLEMENTING:
            self._state.record_illegal(
                f"非法转移: {self._state.stage.value} --authorize_test_deploy--> "
                "TestDeploying。当前阶段不能授权测试发布"
            )
            return
        self._state.test_deploy_authorized = True
        self._state.ticket_event = True

    @workflow.signal
    async def review_override(self, payload: ReviewOverride) -> None:
        """Route a human review decision to the named child ticket."""
        await self._route_to_child(payload.ticket_id, "review_override", payload)

    @workflow.signal
    async def retry_ticket(self, ticket_id: str) -> None:
        """Clear an unhandled failure and ask the child to retry from Implementing."""
        snap = self._state.tickets.get(ticket_id)
        if snap is not None:
            snap.unhandled_failure = False
            self._state.ticket_event = True
        await self._route_to_child(ticket_id, "retry_ticket")

    @workflow.signal
    async def drop_ticket(self, ticket_id: str) -> None:
        """Remove a ticket from the list so it no longer blocks TestDeploying or Retro."""
        snap = self._state.tickets.get(ticket_id)
        if snap is not None:
            snap.unhandled_failure = False
            snap.stage = TicketStage.DROPPED
            self._state.ticket_event = True
        await self._route_to_child(ticket_id, "drop_ticket")

    @workflow.signal
    def ticket_state_changed(self, update: TicketStateUpdate) -> None:
        """Accept a progress update from a child TicketWorkflow."""
        snap = self._state.tickets.get(update.ticket_id)
        if snap is None:
            return
        snap.stage = update.stage
        snap.detail = update.detail
        snap.started = True
        if update.stage is TicketStage.DROPPED:
            snap.unhandled_failure = False
        else:
            snap.unhandled_failure = update.unhandled_failure
        self._state.ticket_event = True

    async def _route_to_child(
        self, ticket_id: str, signal_name: str, payload: object | None = None
    ) -> None:
        handle = self._children.get(ticket_id)
        if handle is None:
            if self._state.input is None:
                return
            snap = self._state.tickets.get(ticket_id)
            if snap is None or not snap.started:
                return
            child_id = ticket_workflow_id(ticket_id, self._state.input.execution_id)
            handle = cast(
                ChildWorkflowHandle[Any, TicketResult],
                workflow.get_external_workflow_handle(child_id),
            )
        if payload is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, payload)
