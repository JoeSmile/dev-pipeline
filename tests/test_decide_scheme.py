"""WorkflowEnvironment tests for DP-17 decide_scheme records and gates."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import (
    initiated_child_workflow_ids,
    scheduled_activity_names,
    started_child_workflow_ids,
)
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    scheme_decision,
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
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio

_DECIDED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    """Real local test server for cross-execution reopen behavior."""
    cli = os.environ.get("TEMPORAL_DEV_SERVER_PATH", "").strip() or None
    kwargs = {"ui": False}
    if cli:
        kwargs["dev_server_existing_path"] = cli
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        yield env


def _story(config_root: Path) -> StoryInput:
    payload = story(
        config_root,
        [TicketSpec(ticket_id="A")],
        auto_start_children=False,
    )
    payload.intake = IntakeRecord(jira_key="DP-17", request="把登录改成单点登录", source="cli")
    return payload


async def _lock(
    handle: object,
) -> None:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.lock_scope,
        scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
    )
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: item.stage is ParentStage.SCOPE_LOCKED
        and item.waiting_signal == "decide_scheme",
    )


async def test_coding_child_cannot_be_dispatched_before_decide_scheme(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        assert snap.scheme is None
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        await _lock(handle)
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        assert snap.scheme is None
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_valid_decide_scheme_without_red_line_reaches_tickets_ready(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await _lock(handle)
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(payload, decided_by="郭伟", tradeoff="采用单点登录，不重写账号库"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY and item.scheme is not None,
        )
        assert snap.scheme is not None
        assert snap.scheme.tradeoff == "采用单点登录，不重写账号库"
        assert [item.grade for item in snap.scheme.hypotheses] == ["局部"]
        assert snap.scheme.hypotheses[0].statement == "局部接口可替换"
        assert snap.scheme.decided_by == "郭伟"
        assert _DECIDED_AT.match(snap.scheme.decided_at)
        assert snap.scheme.retrieval == "桩"
        assert snap.scheme.has_high_risk_assumption is False
        assert snap.waiting_signal is None
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_valid_decide_scheme_with_red_line_enters_spike(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await _lock(handle)
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
            lambda item: item.stage is ParentStage.SPIKE and item.scheme is not None,
        )
        assert snap.scheme is not None
        assert snap.scheme.tradeoff == "先验证红线假设，再决定是否拆票"
        assert [item.grade for item in snap.scheme.hypotheses] == ["红线级"]
        assert snap.scheme.decided_by == "郭伟"
        assert _DECIDED_AT.match(snap.scheme.decided_at)
        assert snap.scheme.retrieval == "桩"
        assert snap.scheme.has_high_risk_assumption is True
        assert snap.waiting_signal == "approve_spike"
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert started_child_workflow_ids(history) == []
        assert "run_spike" not in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_missing_fields_and_invalid_grades_are_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await _lock(handle)
        await handle.signal(DevPipelineWorkflow.decide_scheme, {"tradeoff": "只有取舍"})
        snap = await wait_snapshot(
            handle,
            lambda item: item.last_rejection is not None
            and item.last_rejection.signal == "decide_scheme",
        )
        assert snap.stage is ParentStage.SCOPE_LOCKED
        assert snap.scheme is None
        assert snap.waiting_signal == "decide_scheme"
        assert "必填" in (snap.last_rejection.reason if snap.last_rejection else "")
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            {
                "tradeoff": "采用 A 方案",
                "hypotheses": [{"statement": "未知风险", "grade": "高危"}],
                "decided_by": "郭伟",
            },
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.last_rejection is not None
            and "红线级、局部或候选" in item.last_rejection.reason,
        )
        assert snap.stage is ParentStage.SCOPE_LOCKED
        assert snap.scheme is None
        assert started(snap, "A") is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_decide_scheme_before_lock_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        snap = await wait_snapshot(
            handle,
            lambda item: item.last_rejection is not None
            and item.last_rejection.signal == "decide_scheme",
        )
        assert snap.stage is ParentStage.INTAKE
        assert snap.scheme is None
        assert snap.waiting_signal == "lock_scope"
        assert started(snap, "A") is False
    finally:
        await worker.__aexit__(None, None, None)


async def test_client_supplied_time_and_retrieval_hit_are_ignored(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await _lock(handle)
        forged_time = "1999-01-01T00:00:00Z"
        decision = scheme_decision(payload, decided_by="郭伟")
        decision.decided_at = forged_time
        decision.retrieval = "真实命中"
        await handle.signal(DevPipelineWorkflow.decide_scheme, decision)
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.TICKETS_READY)
        assert snap.scheme is not None
        assert snap.scheme.decided_at != forged_time
        assert _DECIDED_AT.match(snap.scheme.decided_at)
        assert snap.scheme.retrieval == "桩"
    finally:
        await worker.__aexit__(None, None, None)


async def test_reopen_does_not_inherit_scheme_decision(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(local_env, payload, StubController())
    try:
        await _lock(handle)
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(payload, decided_by="郭伟", tradeoff="本轮拍板"),
        )
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.TICKETS_READY)
        assert snap.scheme is not None
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-after-scheme"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-reopen-scheme"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        new_handle = local_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        new_snap = await wait_snapshot(
            new_handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED
            and item.waiting_signal == "decide_scheme"
            and item.scope is not None,
        )
        assert new_snap.scheme is None
        assert new_snap.execution_id == new_id
    finally:
        await worker.__aexit__(None, None, None)

