"""Live Temporal History proof for DP-12 budget gate.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running server
(for example host.docker.internal:7233) instead of binding another 7233.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import story, wait_snapshot, wait_terminal

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.budget import CostlyPathRequest
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import AbortRequest, TicketSpec, story_workflow_id
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

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


async def test_live_breach_does_not_schedule_a_second_costly_activity(
    live_client: Client, tmp_config_root: Path
) -> None:
    (tmp_config_root / "budget.toml").write_text(
        'units = ["calls"]\n[limits]\ncalls = 1\n',
        encoding="utf-8",
    )
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")], suffix=uuid.uuid4().hex[:8])
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-budget-live-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    try:
        handle = await live_client.start_workflow(
            DevPipelineWorkflow.run,
            payload,
            id=story_workflow_id(payload.story_id, payload.execution_id),
            task_queue=task_queue,
        )
        await wait_snapshot(handle, lambda item: item.budget is not None, timeout=30.0)
        first = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="rag"),
        )
        assert first.started is True
        second = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="model"),
        )
        assert second.started is False
        assert "Bad" in second.reason
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        assert snap.budget.last_breach is not None
        assert snap.budget.last_breach.bad == "Bad"
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert names.count("run_rag") == 1
        assert "run_model_call" not in names
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-budget-stop"))
        await wait_terminal(handle, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)
