"""WorkflowEnvironment tests: parent stage changes are mirrored to a fake Jira issue."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import start_parent, wait_snapshot

from dev_pipeline.activities.jira_mirror import mark_jira_mirror_lag, mirror_parent_stage
from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.jira_mirror import LAG_MARKER_LABEL, LAG_MARKER_PREFIX, STAGE_MIRROR_PREFIX
from dev_pipeline.models import (
    IntakeRecord,
    PipelineResult,
    PipelineSnapshot,
    ScopeLock,
    StoryInput,
    TicketSpec,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = pytest.mark.asyncio

_FIXTURE_KEY = "DP-9999"


def _story(config_root: Path, *, suffix: str = "mirror") -> StoryInput:
    return StoryInput(
        story_id=_FIXTURE_KEY,
        execution_id=f"exec-{suffix}",
        config_root=str(config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="jira-mirror-test",
        intake=IntakeRecord(jira_key=_FIXTURE_KEY, source="cli"),
    )


async def _wait_mirrored(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    stage: ParentStage,
) -> PipelineSnapshot:
    return await wait_snapshot(
        handle,
        lambda snap: snap.stage is stage
        and snap.jira_mirror is not None
        and snap.jira_mirror.last_mirrored_stage == stage.value
        and snap.jira_mirror.lagged is False,
    )


async def _lock_scope(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
) -> PipelineSnapshot:
    await handle.signal(
        DevPipelineWorkflow.lock_scope,
        ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
    )
    return await wait_snapshot(handle, lambda snap: snap.stage is ParentStage.SCOPE_LOCKED)


async def test_stage_change_mirrors_current_stage_onto_fake_jira(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="happy")
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _wait_mirrored(handle, ParentStage.INTAKE)
        assert controller.jira.current_stage_text(_FIXTURE_KEY) == "Intake"
        await _lock_scope(handle)
        snap = await _wait_mirrored(handle, ParentStage.SCOPE_LOCKED)
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.lag_marker is None
        comments = controller.jira.comments(_FIXTURE_KEY)
        assert len(comments) == 1
        assert comments[0].body.startswith(STAGE_MIRROR_PREFIX)
        assert "ScopeLocked" in comments[0].body
        assert controller.jira.current_stage_text(_FIXTURE_KEY) == "ScopeLocked"
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert names.count("mirror_parent_stage") >= 2
    finally:
        await worker.__aexit__(None, None, None)


async def test_snapshot_exposes_last_mirrored_stage_and_clears_lag_on_success(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="snap")
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        snap = await _wait_mirrored(handle, ParentStage.INTAKE)
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.last_mirrored_stage == "Intake"
        assert snap.jira_mirror.lagged is False
        await _lock_scope(handle)
        snap = await _wait_mirrored(handle, ParentStage.SCOPE_LOCKED)
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.last_mirrored_stage == "ScopeLocked"
        assert snap.jira_mirror.lagged is False
        assert snap.jira_mirror.lag_reason is None
    finally:
        await worker.__aexit__(None, None, None)


async def test_retries_then_records_lag_and_attempts_lag_marker(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="lag")
    controller = StubController()
    controller.jira_always_fail = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE
            and item.jira_mirror is not None
            and item.jira_mirror.lagged is True,
        )
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.lag_marker == LAG_MARKER_LABEL
        assert snap.jira_mirror.last_mirrored_stage is None
        assert snap.stage is ParentStage.INTAKE
        assert controller.jira_mirror_attempts >= 3
        assert len(controller.jira_lag_calls) == 1
        comments = controller.jira.comments(_FIXTURE_KEY)
        assert any(item.body.startswith(LAG_MARKER_PREFIX) for item in comments)
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert "mirror_parent_stage" in names
        assert "mark_jira_mirror_lag" in names
    finally:
        await worker.__aexit__(None, None, None)


async def test_transient_failures_are_retried_until_success(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="retry")
    controller = StubController()
    controller.jira_fail_remaining = 2
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        snap = await _wait_mirrored(handle, ParentStage.INTAKE)
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.lagged is False
        assert controller.jira_mirror_attempts == 3
        assert controller.jira.current_stage_text(_FIXTURE_KEY) == "Intake"
        assert controller.jira_lag_calls == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_missing_jira_config_ends_in_lag_and_does_not_change_stage(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="noconfig")
    controller = StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-noconfig"
    starter = ReopenStarter(workflow_env.client)
    stub_acts = [
        activity
        for activity in controller.activities()
        if getattr(activity, "__name__", "")
        not in {"mirror_parent_stage", "mark_jira_mirror_lag"}
    ]
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[
            *stub_acts,
            starter.start_reopened_workflow,
            mirror_parent_stage,
            mark_jira_mirror_lag,
        ],
    )
    await worker.__aenter__()
    try:
        handle = await workflow_env.client.start_workflow(
            DevPipelineWorkflow.run,
            payload,
            id=f"story-{payload.story_id}-{payload.execution_id}",
            task_queue=task_queue,
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE
            and item.jira_mirror is not None
            and item.jira_mirror.lagged is True,
        )
        assert snap.stage is ParentStage.INTAKE
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.lag_marker == LAG_MARKER_LABEL
        assert "jira.toml" in (snap.jira_mirror.lag_reason or "")
        assert "缺少" in (snap.jira_mirror.lag_reason or "")
    finally:
        await worker.__aexit__(None, None, None)


async def test_stale_jira_value_does_not_change_workflow_stage(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = _story(tmp_config_root, suffix="stale")
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _wait_mirrored(handle, ParentStage.INTAKE)
        await _lock_scope(handle)
        await _wait_mirrored(handle, ParentStage.SCOPE_LOCKED)
        controller.jira.set_field(_FIXTURE_KEY, "customfield_12345", "Done")
        # Overwrite the visible comment text as if a human edited the issue.
        comments = controller.jira.comments(_FIXTURE_KEY)
        controller.jira.update_comment(
            _FIXTURE_KEY,
            comments[0].comment_id,
            f"{STAGE_MIRROR_PREFIX}\n当前阶段: Done\nexecution_id: {payload.execution_id}\n",
        )
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.stage is ParentStage.SCOPE_LOCKED
        assert snap.jira_mirror is not None
        assert snap.jira_mirror.last_mirrored_stage == "ScopeLocked"
        history = await handle.fetch_history()
        assert all("jira" not in name.lower() for name in signaled_names(history))
    finally:
        await worker.__aexit__(None, None, None)


async def test_parent_has_no_inbound_jira_signal_or_update() -> None:
    handlers = [
        name
        for name, member in inspect.getmembers(DevPipelineWorkflow, predicate=inspect.isfunction)
        if "jira" in name.lower()
        and (
            getattr(member, "__temporal_signal_definition", None) is not None
            or getattr(member, "__temporal_update_definition", None) is not None
        )
    ]
    assert handlers == []
    assert "lock_scope" in dir(DevPipelineWorkflow)
    assert "start_costly_path" in dir(DevPipelineWorkflow)


async def test_without_jira_key_no_mirror_activity_is_scheduled(
    workflow_env: WorkflowEnvironment,
    tmp_config_root: Path,
) -> None:
    payload = StoryInput(
        story_id="STORY-no-jira",
        execution_id="exec-no-jira",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="no-jira",
    )
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.INTAKE)
        await _lock_scope(handle)
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        assert snap.jira_mirror is None
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert "mirror_parent_stage" not in names
        assert "mark_jira_mirror_lag" not in names
        assert controller.jira_mirror_calls == []
    finally:
        await worker.__aexit__(None, None, None)
