"""Live Temporal History proof for DP-31 default-off remote writes.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway probe workflow and the in-memory fake journal. Does not call a
live Cursor CLI, write a real Jira issue, or publish to a real environment.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker
from tests.workflow_helpers import exception_text

from dev_pipeline.activities.stubs import publish_to_test_env
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import PublishRequest
from dev_pipeline.remote_write_store import recorded_rejections, reset_recorded_rejections
from dev_pipeline.workflows.production_publish_probe import ProductionPublishProbe

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


async def test_live_production_publish_is_rejected_and_recorded(
    live_client: Client,
) -> None:
    reset_recorded_rejections()
    suffix = uuid.uuid4().hex[:8]
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-dp31-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[ProductionPublishProbe],
        activities=[publish_to_test_env],
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        ProductionPublishProbe.run,
        PublishRequest(
            story_id=f"STORY-dp31-{suffix}",
            execution_id=f"exec-{suffix}",
            target="production",
        ),
        id=f"dp31-prod-publish-{suffix}",
        task_queue=task_queue,
        execution_timeout=timedelta(seconds=30),
    )
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        assert "禁止生产发布" in exception_text(exc.value)
        journal = recorded_rejections()
        assert journal
        assert journal[-1].kind == "production"
        assert journal[-1].action == "publish"
        assert journal[-1].performed is False
        history = await handle.fetch_history()
        text = str(history)
        assert "publish_to_test_env" in text
        assert "CURSOR_API_KEY" not in text
    finally:
        await worker.__aexit__(None, None, None)
        reset_recorded_rejections()
