"""WorkflowEnvironment tests for DP-10 human-signal rejection and reopen."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    scheme_decision,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.models import (
    AbortRequest,
    ApproveSpikeRequest,
    ReopenRequest,
    ReviewOverride,
    ScopeLock,
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.retro import PromoteRuleRequest
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    """Real local test server, without time skipping, for cross-execution reopen."""
    cli = os.environ.get("TEMPORAL_DEV_SERVER_PATH", "").strip() or None
    kwargs = {"ui": False}
    if cli:
        kwargs["dev_server_existing_path"] = cli
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        yield env


async def test_wrong_stage_signals_are_rejected_and_stage_stays(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "decide_scheme"
            ),
        )
        assert snap.stage is ParentStage.INTAKE
        await handle.signal(
            DevPipelineWorkflow.approve_spike,
            ApproveSpikeRequest(accepted=True),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "approve_spike"
            ),
        )
        assert snap.stage is ParentStage.INTAKE
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
        )
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="again", out_of_scope="out", locked_by="tester"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "lock_scope"
            ),
        )
        assert snap.stage is ParentStage.SCOPE_LOCKED
    finally:
        await worker.__aexit__(None, None, None)


async def test_illegal_payload_is_rejected_and_workflow_keeps_running(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, {"out_of_scope": "only"})
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "lock_scope"
            ),
        )
        assert snap.stage is ParentStage.INTAKE
        assert "必填" in (snap.last_rejection.reason if snap.last_rejection else "")
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
        )
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        assert snap.stage is ParentStage.SCOPE_LOCKED
    finally:
        await worker.__aexit__(None, None, None)


async def test_review_override_rejects_unknown_or_non_gate_ticket(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="NOPE", decision="approve", decided_by="tester"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "review_override"
            ),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert "未知 Ticket" in snap.last_rejection.reason
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="A", decision="approve", decided_by="tester"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and "不在 HumanGate" in item.last_rejection.reason
            ),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
    finally:
        await worker.__aexit__(None, None, None)


async def test_promote_rule_schema_error_differs_from_automatic_refusal(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            PromoteRuleRequest(
                candidate_id="skill",
                target="L0",
                reason="已有两次真实命中",
                evidence_citation="docs/knowledge/candidates/skill.md",
                automatic=True,
            ),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.promote_rule_status is not None and "拒绝" in (item.promote_rule_status or "")
            ),
        )
        assert snap.last_rejection is None
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(DevPipelineWorkflow.promote_rule, {"automatic": False})
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "promote_rule"
            ),
        )
        assert "必填" in snap.last_rejection.reason
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.promote_rule_status is not None and "拒绝" in snap.promote_rule_status
    finally:
        await worker.__aexit__(None, None, None)


async def test_abort_ends_execution_reopen_same_id_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-now"))
        snap = await wait_terminal(handle)
        assert snap.stage is ParentStage.BLOCKED
        await handle.signal(
            DevPipelineWorkflow.reopen,
            ReopenRequest(execution_id=payload.execution_id),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "reopen"
                and "新的 execution_id" in item.last_rejection.reason
            ),
        )
        assert snap.stage is ParentStage.BLOCKED
        assert snap.reopened_execution_id is None
    finally:
        await worker.__aexit__(None, None, None)


async def test_reopen_starts_new_independent_parent_execution(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(local_env, payload, StubController())
    try:
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-for-reopen"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-reopen"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.execution_id == payload.execution_id
        new_handle = local_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        new_snap = await new_handle.query(DevPipelineWorkflow.snapshot)
        assert new_snap.story_id == payload.story_id
        assert new_snap.execution_id == new_id
        assert new_snap.stage is ParentStage.INTAKE
        await new_handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="reopened", out_of_scope="out", locked_by="tester"),
        )
        new_snap = await wait_snapshot(
            new_handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED
        )
        assert new_snap.execution_id == new_id
        assert new_snap.stage is ParentStage.SCOPE_LOCKED
        result = await handle.result()
        assert result.stage is ParentStage.BLOCKED
        assert result.execution_id == payload.execution_id
    finally:
        await worker.__aexit__(None, None, None)


async def test_reopen_rejects_when_target_workflow_id_exists(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-taken"
        taken = StoryInput(
            story_id=payload.story_id,
            execution_id=new_id,
            config_root=payload.config_root,
            tickets=list(payload.tickets),
            summary=payload.summary,
        )
        description = await handle.describe()
        await workflow_env.client.start_workflow(
            DevPipelineWorkflow.run,
            taken,
            id=story_workflow_id(payload.story_id, new_id),
            task_queue=description.task_queue,
        )
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "reopen"
                and "已存在" in item.last_rejection.reason
            ),
        )
        assert snap.stage is ParentStage.BLOCKED
        assert snap.reopened_execution_id is None
        taken_handle = workflow_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        taken_snap = await taken_handle.query(DevPipelineWorkflow.snapshot)
        assert taken_snap.stage is ParentStage.INTAKE
    finally:
        await worker.__aexit__(None, None, None)
