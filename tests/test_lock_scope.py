"""WorkflowEnvironment tests for DP-16 lock_scope recording and gates."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import initiated_child_workflow_ids, started_child_workflow_ids
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    scope_lock,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.models import (
    AbortRequest,
    IntakeRecord,
    ReopenRequest,
    ScopeLock,
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio

_LOCKED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    """Real local test server for cross-execution reopen inheritance."""
    cli = os.environ.get("TEMPORAL_DEV_SERVER_PATH", "").strip() or None
    kwargs = {"ui": False}
    if cli:
        kwargs["dev_server_existing_path"] = cli
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        yield env


def _story_with_gaps(config_root: Path) -> StoryInput:
    payload = story(config_root, [TicketSpec(ticket_id="A")])
    payload.intake = IntakeRecord(jira_key="DP-16", request="把登录改成单点登录", source="cli")
    payload.summary = "不要用这段"
    return payload


async def test_coding_path_and_child_dispatch_unreachable_before_lock(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        assert snap.waiting_signal == "lock_scope"
        assert snap.scope is None
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_valid_lock_scope_writes_record_and_stays_scope_locked(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story_with_gaps(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        assert snap.story_draft is not None
        assert snap.story_draft.complete is False
        assert "待证" in snap.story_draft.missing_items[0]
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED
            and item.waiting_signal == "decide_scheme",
        )
        assert snap.scope is not None
        assert snap.scope.in_scope == "登录单点"
        assert snap.scope.out_of_scope == "不改计费"
        assert snap.scope.locked_by == "郭伟"
        assert _LOCKED_AT.match(snap.scope.locked_at)
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)

async def test_missing_required_lock_fields_are_rejected_and_stage_stays_intake(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        await handle.signal(DevPipelineWorkflow.lock_scope, {"in_scope": "只要解决"})
        snap = await wait_snapshot(
            handle,
            lambda item: item.last_rejection is not None
            and item.last_rejection.signal == "lock_scope",
        )
        assert snap.stage is ParentStage.INTAKE
        assert snap.scope is None
        assert snap.waiting_signal == "lock_scope"
        assert "必填" in (snap.last_rejection.reason if snap.last_rejection else "")
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_client_supplied_locked_at_is_ignored(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        forged = "1999-01-01T00:00:00Z"
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(
                in_scope="in",
                out_of_scope="out",
                locked_by="tester",
                locked_at=forged,
            ),
        )
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        assert snap.scope is not None
        assert snap.scope.locked_at != forged
        assert _LOCKED_AT.match(snap.scope.locked_at)
    finally:
        await worker.__aexit__(None, None, None)


async def test_reopen_inherits_locked_scope_read_only(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(local_env, payload, StubController())
    try:
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
        )
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        assert snap.scope is not None
        locked = snap.scope
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-after-lock"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-reopen-scope"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        assert snap.stage is ParentStage.BLOCKED
        new_handle = local_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        new_snap = await wait_snapshot(
            new_handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED
            and item.waiting_signal == "decide_scheme"
            and item.scope is not None,
        )
        assert new_snap.execution_id == new_id
        assert new_snap.scope is not None
        assert new_snap.scope.in_scope == locked.in_scope
        assert new_snap.scope.out_of_scope == locked.out_of_scope
        assert new_snap.scope.locked_by == locked.locked_by
        assert new_snap.scope.locked_at == locked.locked_at
        await new_handle.signal(
            DevPipelineWorkflow.lock_scope,
            scope_lock(in_scope="改掉范围", out_of_scope="改掉非目标", locked_by="他人"),
        )
        rejected = await wait_snapshot(
            new_handle,
            lambda item: item.last_rejection is not None
            and item.last_rejection.signal == "lock_scope",
        )
        assert rejected.stage is ParentStage.SCOPE_LOCKED
        assert rejected.scope is not None
        assert rejected.scope.in_scope == locked.in_scope
        assert rejected.scope.out_of_scope == locked.out_of_scope
        assert rejected.scope.locked_by == locked.locked_by
        assert rejected.scope.locked_at == locked.locked_at
        history = await new_handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)
