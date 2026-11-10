"""Configurable activity replacements for WorkflowEnvironment tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from temporalio import activity

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
)
from dev_pipeline.transitions import validate_publish_target


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

        @activity.defn(name="run_spike")
        async def run_spike(payload: SpikeInput) -> SpikeResult:
            return SpikeResult(red_line_failed=controller.spike_red_line, detail="test-spike")

        @activity.defn(name="implement_ticket")
        async def implement_ticket(payload: TicketWorkInput) -> ImplementResult:
            controller.started.append(payload.ticket_id)
            if payload.ticket_id in controller.implement_errors:
                raise RuntimeError(f"implement failed: {payload.ticket_id}")
            await controller.gate("implement", payload.ticket_id).wait()
            return ImplementResult(diff_submitted=True, detail="test-implement")

        @activity.defn(name="review_ticket")
        async def review_ticket(payload: TicketWorkInput) -> ReviewFindings:
            await controller.gate("review", payload.ticket_id).wait()
            return controller.review_findings.get(payload.ticket_id, ReviewFindings(info=["ok"]))

        @activity.defn(name="verify_ticket")
        async def verify_ticket(payload: TicketWorkInput) -> VerifyResult:
            await controller.gate("verify", payload.ticket_id).wait()
            passed = controller.verify_passed.get(payload.ticket_id, True)
            return VerifyResult(passed=passed, evidence="test-evidence")

        @activity.defn(name="publish_to_test_env")
        async def publish_to_test_env(request: PublishRequest) -> PublishResult:
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

        return (
            validate_config_root,
            run_spike,
            implement_ticket,
            review_ticket,
            verify_ticket,
            publish_to_test_env,
            write_retro_record,
        )
