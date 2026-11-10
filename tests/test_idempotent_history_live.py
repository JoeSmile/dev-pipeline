"""Live Temporal History proof for DP-11 idempotent child starts.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running server
(for example host.docker.internal:7233) instead of binding another 7233.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import WorkflowIdReusePolicy
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.common import WorkflowIDReusePolicy as SdkReuse
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker
from tests.history_events import (
    child_id_reuse_policies,
    started_child_workflow_ids,
)
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    started,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    TicketInput,
    TicketSpec,
    story_workflow_id,
    ticket_workflow_id,
)
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"
REJECT_DUPLICATE = int(WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE)


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def test_live_same_key_reentry_records_a_single_child_id(
    live_client: Client, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")], suffix=uuid.uuid4().hex[:8])
    controller = StubController()
    controller.hold("implement", "A")
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-idempotent-live-{uuid.uuid4().hex[:8]}"
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
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"), timeout=30.0)
        await wait_until(
            lambda: controller.workspace.get("A", payload.execution_id) is not None,
            timeout=30.0,
        )
        child_id = ticket_workflow_id("A", payload.execution_id)
        history = await handle.fetch_history()
        assert started_child_workflow_ids(history) == [child_id]
        assert child_id_reuse_policies(history) == [REJECT_DUPLICATE]
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        with pytest.raises(WorkflowAlreadyStartedError):
            await live_client.start_workflow(
                TicketWorkflow.run,
                TicketInput(
                    ticket_id="A",
                    execution_id=payload.execution_id,
                    story_id=payload.story_id,
                    config_root=payload.config_root,
                    parent_workflow_id="",
                    depends_on=[],
                ),
                id=child_id,
                task_queue=task_queue,
                id_reuse_policy=SdkReuse.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-idempotent-stop"))
        await wait_terminal(handle, timeout=30.0)
        history = await handle.fetch_history()
        assert started_child_workflow_ids(history) == [child_id]
    finally:
        await worker.__aexit__(None, None, None)
