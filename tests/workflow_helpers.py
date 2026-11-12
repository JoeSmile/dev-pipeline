"""Shared helpers for WorkflowEnvironment tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.stub_activities import StubController

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import DEFAULT_TASK_QUEUE, WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import (
    PipelineResult,
    PipelineSnapshot,
    SchemeDecision,
    SchemeHypothesis,
    ScopeLock,
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow


async def wait_snapshot(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    predicate: object,
    *,
    timeout: float = 15.0,
) -> PipelineSnapshot:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: PipelineSnapshot | None = None
    while loop.time() < deadline:
        last = await handle.query(DevPipelineWorkflow.snapshot)
        if predicate(last):  # type: ignore[operator]
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for snapshot, last={last}")


def scope_lock(
    in_scope: str = "in",
    out_of_scope: str = "out",
    locked_by: str = "tester",
) -> ScopeLock:
    """Build a valid lock_scope payload for workflow tests."""
    return ScopeLock(in_scope=in_scope, out_of_scope=out_of_scope, locked_by=locked_by)


def complete_ticket(ticket_id: str, depends_on: list[str] | None = None) -> TicketSpec:
    """Build a complete required ticket for tests that enter Implementing."""
    return TicketSpec(
        ticket_id=ticket_id,
        goal=f"实现 {ticket_id}",
        acceptance=f"{ticket_id} 的验收通过",
        depends_on=list(depends_on or []),
        io_contract=f"{ticket_id} 的输入与输出约定",
    )


def story(
    config_root: Path,
    tickets: Sequence[TicketSpec],
    *,
    suffix: str | None = None,
    auto_start_children: bool = True,
) -> StoryInput:
    token = suffix or uuid.uuid4().hex[:8]
    return StoryInput(
        story_id=f"STORY-{token}",
        execution_id=f"exec-{token}",
        config_root=str(config_root),
        tickets=list(tickets),
        summary="test story",
        auto_start_children=auto_start_children,
    )


def scheme_decision(
    payload: StoryInput | None = None,
    *,
    high_risk: bool = False,
    decided_by: str = "tester",
    tradeoff: str = "采用现有方案，不做未提供的技术细节",
    tickets: Sequence[TicketSpec] | None = None,
) -> SchemeDecision:
    """Build a valid decide_scheme payload for workflow tests."""
    hypotheses = (
        [SchemeHypothesis(statement="该假设可能击穿 L0", grade="红线级")]
        if high_risk
        else [SchemeHypothesis(statement="局部接口可替换", grade="局部")]
    )
    specs = list(tickets) if tickets is not None else list(payload.tickets) if payload else []
    return SchemeDecision(
        tradeoff=tradeoff,
        hypotheses=hypotheses,
        decided_by=decided_by,
        tickets=specs,
        has_high_risk_assumption=high_risk,
    )


async def start_parent(
    env: WorkflowEnvironment,
    payload: StoryInput,
    controller: StubController,
    *,
    queue: str | None = None,
) -> tuple[Worker, WorkflowHandle[DevPipelineWorkflow, PipelineResult]]:
    task_queue = queue or f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    starter = ReopenStarter(env.client)
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle


async def advance_to_implementing(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    payload: StoryInput,
    *,
    high_risk: bool = False,
) -> PipelineSnapshot:
    await handle.signal(
        DevPipelineWorkflow.lock_scope,
        scope_lock(),
    )
    await wait_snapshot(
        handle,
        lambda snap: snap.stage in {ParentStage.SCOPE_LOCKED, ParentStage.IMPLEMENTING},
    )
    await handle.signal(
        DevPipelineWorkflow.decide_scheme,
        scheme_decision(payload, high_risk=high_risk),
    )
    return await wait_snapshot(
        handle,
        lambda snap: (
            snap.stage in {ParentStage.IMPLEMENTING, ParentStage.SPIKE, ParentStage.BLOCKED}
        ),
    )


async def authorize_test_deploy(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
) -> PipelineSnapshot:
    snap = await wait_snapshot(
        handle,
        lambda item: (
            item.waiting_signal is not None and WAITING_AUTHORIZE_TEST_DEPLOY in item.waiting_signal
        ),
    )
    await handle.signal(DevPipelineWorkflow.authorize_test_deploy)
    return snap


async def wait_terminal(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    *,
    timeout: float = 15.0,
) -> PipelineSnapshot:
    """Wait until the parent has reached Done or Blocked (business terminal)."""
    return await wait_snapshot(
        handle,
        lambda snap: snap.stage in {ParentStage.DONE, ParentStage.BLOCKED},
        timeout=timeout,
    )


async def wait_until(predicate: object, *, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timeout waiting for condition")


def exception_text(exc: BaseException) -> str:
    chunks: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chunks.append(str(current))
        message = getattr(current, "message", None)
        if message:
            chunks.append(str(message))
        current = getattr(current, "cause", None) or current.__cause__
    return "\n".join(chunks)


def stage_of(snap: PipelineSnapshot, ticket_id: str) -> object:
    for item in snap.tickets:
        if item.ticket_id == ticket_id:
            return item.stage
    return None


def started(snap: PipelineSnapshot, ticket_id: str) -> bool:
    for item in snap.tickets:
        if item.ticket_id == ticket_id:
            return item.started
    return False
