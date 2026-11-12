"""Live Temporal History proof for DP-25 Retro, blocked-from-Retro, and promote_rule.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway knowledge tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
If a child would park (Implementing hold / Failed-retry / waiting signal), the
snapshot is queried and the workflow is aborted after assertions. Does not
terminate an already completed workflow.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    promote_rule_request,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.activities.retro import apply_promote_rule, write_retro_record
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import AbortRequest, story_workflow_id
from dev_pipeline.retro import CANDIDATE_STATUS, CandidateRecord, render_candidate_text
from dev_pipeline.stages import ParentStage
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


def _real_retro_activities(controller: StubController) -> list[object]:
    skip = {"write_retro_record", "apply_promote_rule"}
    kept = [item for item in controller.activities() if getattr(item, "__name__", "") not in skip]
    kept.extend([write_retro_record, apply_promote_rule])
    return kept


async def _start_live(
    client: Client,
    tmp_config_root: Path,
    *,
    suffix: str,
    controller: StubController | None = None,
):
    ctrl = controller or StubController()
    payload = story(
        tmp_config_root,
        [complete_ticket("A")],
        suffix=suffix,
    )
    starter = ReopenStarter(client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-retro-{suffix}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*_real_retro_activities(ctrl), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload, ctrl


async def _finish(handle: object, worker: Worker) -> None:
    try:
        desc = await handle.describe()  # type: ignore[union-attr]
        if desc.status not in {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.FAILED,
        }:
            await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-cleanup"))
            try:
                await wait_terminal(handle, timeout=30.0)  # type: ignore[arg-type]
            except Exception:
                pass
    except Exception:
        pass
    await worker.__aexit__(None, None, None)


async def test_live_retro_reaches_done_and_writes_daily(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload, _ctrl = await _start_live(live_client, tmp_config_root, suffix=suffix)
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle, timeout=30.0)
        assert result.stage is ParentStage.DONE
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.retro is not None
        daily = Path(snap.retro.daily_path)
        assert daily.is_file()
        text = daily.read_text(encoding="utf-8")
        assert payload.story_id in text
        assert "技术难度" not in text
        assert all(item.status == CANDIDATE_STATUS for item in snap.retro.candidates)
        history = await handle.fetch_history()
        assert "write_retro_record" in scheduled_activity_names(history)
        # Parent stays Temporal-RUNNING after business Done so reopen can still
        # be received. Do not wait for handle.result() / COMPLETED here.
    finally:
        await _finish(handle, worker)


async def test_live_unverified_ticket_cannot_enter_retro(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle, payload, _ctrl = await _start_live(
        live_client, tmp_config_root, suffix=suffix, controller=controller
    )
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(handle, lambda item: started(item, "A"), timeout=30.0)
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage not in {ParentStage.RETRO, ParentStage.DONE}
        assert snap.retro is None
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-unverified"))
        result = await wait_terminal(handle, timeout=30.0)
        assert result.stage is ParentStage.BLOCKED
        assert result.stage is not ParentStage.RETRO
        history = await handle.fetch_history()
        assert "write_retro_record" not in scheduled_activity_names(history)
    finally:
        await _finish(handle, worker)


async def test_live_promote_rule_is_recorded_in_history(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    cand_dir = tmp_config_root / "docs" / "knowledge" / "candidates"
    cand_dir.mkdir(parents=True)
    record = CandidateRecord(
        candidate_id="live-cand",
        title="现场可晋升",
        hits=2,
        citations=["evidence/verify.md"],
    )
    (cand_dir / "live-cand.md").write_text(render_candidate_text(record), encoding="utf-8")
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle, payload, _ctrl = await _start_live(
        live_client, tmp_config_root, suffix=suffix, controller=controller
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"), timeout=30.0)
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            promote_rule_request(
                "live-cand",
                target="L0",
                evidence_citation="evidence/verify.md",
            ),
        )
        snap = await wait_snapshot(handle, lambda item: item.promotion is not None, timeout=30.0)
        assert snap.promotion is not None
        assert snap.promotion.candidate_id == "live-cand"
        assert snap.promotion.target == "L0"
        assert "状态: L0" in (cand_dir / "live-cand.md").read_text(encoding="utf-8")
        history = await handle.fetch_history()
        assert "apply_promote_rule" in scheduled_activity_names(history)
        assert "promote_rule" in signaled_names(history)
        controller.release("implement", "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle, timeout=30.0)
        assert result.stage is ParentStage.DONE
    finally:
        await _finish(handle, worker)
