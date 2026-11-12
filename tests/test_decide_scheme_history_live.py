"""Live Temporal History proof for DP-17 decide_scheme.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import (
    initiated_child_workflow_ids,
    scheduled_activity_names,
    signaled_names,
    started_child_workflow_ids,
)
from tests.stub_activities import StubController
from tests.workflow_helpers import scheme_decision, scope_lock, wait_snapshot

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import IntakeRecord, StoryInput, TicketSpec, story_workflow_id
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"
_DECIDED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def _start_live(
    live_client: Client, tmp_config_root: Path, suffix: str
) -> tuple[Worker, object, StoryInput]:
    payload = StoryInput(
        story_id="DP-17",
        execution_id=f"exec-live-scheme-{suffix}",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="不要用这段",
        intake=IntakeRecord(
            jira_key="DP-17",
            request="把登录改成单点登录",
            source="cli",
        ),
        auto_start_children=False,
    )
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-scheme-live-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload


async def _lock_and_wait(handle: object) -> None:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.lock_scope,
        scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
    )
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.SCOPE_LOCKED and item.waiting_signal == "decide_scheme"
        ),
        timeout=30.0,
    )


async def test_live_decide_scheme_without_red_line_reaches_tickets_ready(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, payload = await _start_live(live_client, tmp_config_root, f"ok-{token}")
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_wait(handle)
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(payload, decided_by="郭伟", tradeoff="采用单点登录，不重写账号库"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY and item.scheme is not None,
            timeout=30.0,
        )
        assert snap.scheme is not None
        assert snap.scheme.tradeoff == "采用单点登录，不重写账号库"
        assert [item.grade for item in snap.scheme.hypotheses] == ["局部"]
        assert snap.scheme.decided_by == "郭伟"
        assert _DECIDED_AT.match(snap.scheme.decided_at)
        assert snap.scheme.retrieval == "桩"
        assert snap.scheme.has_high_risk_assumption is False
        assert all(not item.started for item in snap.tickets)
        history = await handle.fetch_history()
        assert "decide_scheme" in signaled_names(history)
        assert "run_spike" not in scheduled_activity_names(history)
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_decide_scheme_with_red_line_runs_spike_to_tickets_ready(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, payload = await _start_live(live_client, tmp_config_root, f"spike-{token}")
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_wait(handle)
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(
                payload,
                high_risk=True,
                decided_by="郭伟",
                tradeoff="先验证红线假设，再决定是否拆票",
            ),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.TICKETS_READY
                and item.scheme is not None
                and item.spike is not None
            ),
            timeout=30.0,
        )
        assert snap.scheme is not None
        assert snap.scheme.tradeoff == "先验证红线假设，再决定是否拆票"
        assert [item.grade for item in snap.scheme.hypotheses] == ["红线级"]
        assert snap.scheme.decided_by == "郭伟"
        assert _DECIDED_AT.match(snap.scheme.decided_at)
        assert snap.scheme.retrieval == "桩"
        assert snap.scheme.has_high_risk_assumption is True
        assert snap.spike is not None
        assert snap.spike.conclusion == "验证过"
        assert snap.waiting_signal is None
        assert all(not item.started for item in snap.tickets)
        history = await handle.fetch_history()
        assert "decide_scheme" in signaled_names(history)
        assert "run_spike" in scheduled_activity_names(history)
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_decide_scheme_rejects_missing_fields_and_stays_scope_locked(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, _payload = await _start_live(live_client, tmp_config_root, f"rej-{token}")
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_wait(handle)
        await handle.signal(DevPipelineWorkflow.decide_scheme, {"tradeoff": "只有取舍"})
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "decide_scheme"
            ),
            timeout=30.0,
        )
        assert snap.stage is ParentStage.SCOPE_LOCKED
        assert snap.scheme is None
        assert snap.waiting_signal == "decide_scheme"
        assert "必填" in (snap.last_rejection.reason if snap.last_rejection else "")
        history = await handle.fetch_history()
        assert "decide_scheme" in signaled_names(history)
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)
