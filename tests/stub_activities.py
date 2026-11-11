"""Configurable activity replacements for WorkflowEnvironment tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from temporalio import activity

from dev_pipeline.budget import BudgetAmounts, BudgetConfig, CostlyPathInput, CostlyPathResult
from dev_pipeline.models import (
    ImplementResult,
    PublishRequest,
    PublishResult,
    RetroInput,
    RetroResult,
    ReviewFindings,
    SpikeInput,
    SpikeResult,
    TicketWorkInput,
    VerifyResult,
    WorkspaceClaim,
)
from dev_pipeline.transitions import validate_publish_target
from dev_pipeline.workspace import WorkspaceClaimStore, claim_workspace


async def _await_gate(event: asyncio.Event) -> None:
    """Wait for a test gate, heartbeating so workflow cancellation can land."""
    while not event.is_set():
        if activity.is_cancelled():
            raise asyncio.CancelledError()
        activity.heartbeat()
        try:
            await asyncio.wait_for(event.wait(), timeout=0.05)
        except TimeoutError:
            continue


@dataclass
class StubController:
    """Per-test switches and gates for stub activities."""

    implement_gate: dict[str, asyncio.Event] = field(default_factory=dict)
    review_gate: dict[str, asyncio.Event] = field(default_factory=dict)
    verify_gate: dict[str, asyncio.Event] = field(default_factory=dict)
    implement_errors: set[str] = field(default_factory=set)
    review_findings: dict[str, ReviewFindings] = field(default_factory=dict)
    verify_passed: dict[str, bool] = field(default_factory=dict)
    spike_red_line: bool = False
    publish_succeeds: bool = True
    publish_address: str = "http://test.local/stub"
    started: list[str] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)
    publish_calls: int = 0
    implement_fail_remaining: dict[str, int] = field(default_factory=dict)
    workspace: WorkspaceClaimStore = field(default_factory=WorkspaceClaimStore)
    costly_queue: dict[str, list[CostlyPathResult]] = field(default_factory=dict)
    costly_started: list[str] = field(default_factory=list)

    def queue_costly(self, path: str, result: CostlyPathResult) -> None:
        self.costly_queue.setdefault(path, []).append(result)

    def _next_costly(self, path: str) -> CostlyPathResult:
        queue = self.costly_queue.get(path, [])
        if queue:
            return queue.pop(0)
        return CostlyPathResult(
            path=path,
            has_new_evidence=True,
            usage=BudgetAmounts(calls=1),
            detail=f"test-{path}",
        )

    def gate(self, kind: str, ticket_id: str) -> asyncio.Event:
        store = {
            "implement": self.implement_gate,
            "review": self.review_gate,
            "verify": self.verify_gate,
        }[kind]
        event = store.get(ticket_id)
        if event is None:
            event = asyncio.Event()
            # Gates are open unless a test explicitly holds that ticket/kind.
            event.set()
            store[ticket_id] = event
        return event

    def hold(self, kind: str, *ticket_ids: str) -> None:
        for ticket_id in ticket_ids:
            event = self.gate(kind, ticket_id)
            event.clear()

    def release(self, kind: str, *ticket_ids: str) -> None:
        for ticket_id in ticket_ids:
            self.gate(kind, ticket_id).set()

    def activities(self) -> Sequence[Callable[..., object]]:
        controller = self

        @activity.defn(name="validate_config_root")
        async def validate_config_root(config_root: str) -> str:
            from dev_pipeline.config_root import validate_config_root_path

            return validate_config_root_path(config_root)

        @activity.defn(name="load_budget_config")
        async def load_budget_config(config_root: str) -> BudgetConfig:
            from dev_pipeline.budget_config import load_budget_config_from_path

            return load_budget_config_from_path(config_root)

        @activity.defn(name="run_spike")
        async def run_spike(payload: SpikeInput) -> SpikeResult:
            return SpikeResult(red_line_failed=controller.spike_red_line, detail="test-spike")

        @activity.defn(name="claim_ticket_workspace")
        async def claim_ticket_workspace(payload: TicketWorkInput) -> WorkspaceClaim:
            return claim_workspace(payload, store=controller.workspace)

        @activity.defn(name="implement_ticket")
        async def implement_ticket(payload: TicketWorkInput) -> ImplementResult:
            claim_workspace(payload, store=controller.workspace)
            controller.started.append(payload.ticket_id)
            remaining = controller.implement_fail_remaining.get(payload.ticket_id, 0)
            if remaining > 0:
                controller.implement_fail_remaining[payload.ticket_id] = remaining - 1
                raise RuntimeError(f"implement failed: {payload.ticket_id}")
            if payload.ticket_id in controller.implement_errors:
                raise RuntimeError(f"implement failed: {payload.ticket_id}")
            await _await_gate(controller.gate("implement", payload.ticket_id))
            return ImplementResult(diff_submitted=True, detail="test-implement")

        @activity.defn(name="review_ticket")
        async def review_ticket(payload: TicketWorkInput) -> ReviewFindings:
            controller.reviewed.append(payload.ticket_id)
            await _await_gate(controller.gate("review", payload.ticket_id))
            return controller.review_findings.get(payload.ticket_id, ReviewFindings(info=["ok"]))

        @activity.defn(name="verify_ticket")
        async def verify_ticket(payload: TicketWorkInput) -> VerifyResult:
            await _await_gate(controller.gate("verify", payload.ticket_id))
            passed = controller.verify_passed.get(payload.ticket_id, True)
            return VerifyResult(passed=passed, evidence="test-evidence")

        @activity.defn(name="publish_to_test_env")
        async def publish_to_test_env(request: PublishRequest) -> PublishResult:
            controller.publish_calls += 1
            validate_publish_target(request.target)
            if not controller.publish_succeeds:
                return PublishResult(status="failed", address="", detail="test-deploy-failed")
            return PublishResult(
                status="succeeded",
                address=controller.publish_address,
                detail="test-deploy-ok",
            )

        @activity.defn(name="write_retro_record")
        async def write_retro_record(payload: RetroInput) -> RetroResult:
            return RetroResult(recorded=True, detail=payload.story_id)

        @activity.defn(name="run_debate")
        async def run_debate(payload: CostlyPathInput) -> CostlyPathResult:
            controller.costly_started.append("debate")
            return controller._next_costly("debate")

        @activity.defn(name="run_rag")
        async def run_rag(payload: CostlyPathInput) -> CostlyPathResult:
            controller.costly_started.append("rag")
            return controller._next_costly("rag")

        @activity.defn(name="run_model_call")
        async def run_model_call(payload: CostlyPathInput) -> CostlyPathResult:
            controller.costly_started.append("model")
            return controller._next_costly("model")

        return (
            validate_config_root,
            load_budget_config,
            run_spike,
            claim_ticket_workspace,
            implement_ticket,
            review_ticket,
            verify_ticket,
            run_debate,
            run_rag,
            run_model_call,
            publish_to_test_env,
            write_retro_record,
        )
