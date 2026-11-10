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

from dev_pipeline.constants import DEFAULT_TASK_QUEUE, WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import (
    PipelineResult,
    PipelineSnapshot,
    SchemeDecision,
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


def story(
    config_root: Path,
    tickets: Sequence[TicketSpec],
    *,
    suffix: str | None = None,
) -> StoryInput:
    token = suffix or uuid.uuid4().hex[:8]
    return StoryInput(
        story_id=f"STORY-{token}",
        execution_id=f"exec-{token}",
        config_root=str(config_root),
        tickets=list(tickets),
        summary="test story",
    )


async def start_parent(
    env: WorkflowEnvironment,
    payload: StoryInput,
    controller: StubController,
    *,
    queue: str | None = None,
) -> tuple[Worker, WorkflowHandle[DevPipelineWorkflow, PipelineResult]]:
    task_queue = queue or f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=list(controller.activities()),
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
        ScopeLock(in_scope="in", out_of_scope="out"),
    )
    await wait_snapshot(
        handle,
        lambda snap: snap.stage in {ParentStage.SCOPE_LOCKED, ParentStage.IMPLEMENTING},
    )
    await handle.signal(
        DevPipelineWorkflow.decide_scheme,
        SchemeDecision(has_high_risk_assumption=high_risk, tickets=list(payload.tickets)),
    )
    return await wait_snapshot(
        handle,
        lambda snap: snap.stage
        in {ParentStage.IMPLEMENTING, ParentStage.SPIKE, ParentStage.BLOCKED},
    )


async def authorize_test_deploy(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
) -> PipelineSnapshot:
    snap = await wait_snapshot(
        handle,
        lambda item: item.waiting_signal is not None
        and WAITING_AUTHORIZE_TEST_DEPLOY in item.waiting_signal,
    )
    await handle.signal(DevPipelineWorkflow.authorize_test_deploy)
    return snap


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
