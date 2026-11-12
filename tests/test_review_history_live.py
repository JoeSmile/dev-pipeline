"""Live Temporal History proof for DP-22 dual review.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
If a child parks in HumanGate, the snapshot is queried and the workflow is
terminated after assertions.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names, scheduled_before_any_complete
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.test_deliverables_workflow import _wait_child
from tests.test_review_workflow import (
    _activities_real_review,
    _finish,
    planted_oversized_implement,
)

from dev_pipeline.activities.review import review_ticket
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import ReviewFindings, TicketInput, ticket_workflow_id
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows import TicketWorkflow
from dev_pipeline.workspace import reset_default_claim_store

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def _run(client: Client, payload: TicketInput, activities: list[object]):
    suffix = payload.execution_id
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-rev-{suffix}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=activities,
    )
    await worker.__aenter__()
    handle = await client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    return handle, worker


async def test_live_advice_only_reaches_verifying(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    controller = StubController()
    payload = TicketInput(
        ticket_id="T1",
        execution_id=f"exec-live-advice-{suffix}",
        story_id=f"STORY-live-advice-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run(
        live_client, payload, _activities_real_review(controller)
    )
    try:
        snap = await _wait_child(
            handle,
            timeout=30.0,
            stages=frozenset({TicketStage.VERIFYING, TicketStage.VERIFIED}),
        )
        assert snap.stage in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert snap.review is not None
        assert snap.review.destination == "Verifying"
        assert snap.review.requires_human_gate is False
        assert len(snap.review.workers) == 2
        history = await handle.fetch_history()
        assert scheduled_activity_names(history).count("review_ticket") == 2
        assert scheduled_before_any_complete(history, "review_ticket") == 2
        assert "CURSOR_API_KEY" not in str(history)
    finally:
        await _finish(handle, worker)
        reset_default_claim_store()


async def test_live_blocking_review_waits_at_human_gate(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    controller = StubController()
    controller.review_findings["T-block"] = ReviewFindings(blocking=["must gate"])
    payload = TicketInput(
        ticket_id="T-block",
        execution_id=f"exec-live-block-{suffix}",
        story_id=f"STORY-live-block-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run(live_client, payload, list(controller.activities()))
    try:
        snap = await _wait_child(
            handle, timeout=30.0, stages=frozenset({TicketStage.HUMAN_GATE})
        )
        assert snap.stage is TicketStage.HUMAN_GATE
        assert snap.review is not None
        assert snap.review.requires_human_gate is True
        assert snap.review.auto_arbitrated is False
        history = await handle.fetch_history()
        assert scheduled_activity_names(history).count("review_ticket") == 2
        assert "CURSOR_API_KEY" not in str(history)
    finally:
        await _finish(handle, worker)
        reset_default_claim_store()


async def test_live_oversized_file_is_hard_block(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    controller = StubController()
    kept: list[object] = []
    for item in controller.activities():
        name = getattr(item, "__name__", "")
        if name in {"implement_ticket", "review_ticket"}:
            continue
        kept.append(item)
    kept.extend([planted_oversized_implement, review_ticket])
    payload = TicketInput(
        ticket_id="T-huge",
        execution_id=f"exec-live-huge-{suffix}",
        story_id=f"STORY-live-huge-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run(live_client, payload, kept)
    try:
        snap = await _wait_child(
            handle, timeout=30.0, stages=frozenset({TicketStage.HUMAN_GATE})
        )
        assert snap.stage is TicketStage.HUMAN_GATE
        assert snap.review is not None
        assert any(
            item.severity == "阻断" and "2000" in (item.summary + item.rule_citation)
            for item in snap.review.findings
        )
        assert "CURSOR_API_KEY" not in str(await handle.fetch_history())
    finally:
        await _finish(handle, worker)
        reset_default_claim_store()
