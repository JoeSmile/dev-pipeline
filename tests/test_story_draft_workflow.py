"""Parent Intake records a Story draft and stays waiting for lock_scope."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import start_parent, wait_snapshot

from dev_pipeline.models import IntakeRecord, StoryInput
from dev_pipeline.stages import ParentStage


@pytest.mark.asyncio
async def test_intake_records_draft_and_does_not_lock_scope(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = StoryInput(
        story_id="DP-15",
        execution_id="exec-draft",
        config_root=str(tmp_config_root),
        tickets=[],
        summary="不要用这段",
        intake=IntakeRecord(
            jira_key="DP-15",
            request="把登录改成单点登录",
            source="cli",
        ),
    )
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        assert snap.waiting_signal == "lock_scope"
        assert snap.story_draft is not None
        assert snap.story_draft.to_solve == "把登录改成单点登录"
        assert snap.story_draft.known_constraints == "待证"
        assert snap.story_draft.acceptance == "待证"
        assert "已知约束: 待证" in snap.story_draft.missing_items
        assert "验收: 待证" in snap.story_draft.missing_items
        assert snap.story_draft.complete is False
        history = await handle.fetch_history()
        assert "normalize_story_draft" in scheduled_activity_names(history)
        assert "lock_scope" not in signaled_names(history)
        assert snap.stage is ParentStage.INTAKE
    finally:
        await worker.__aexit__(None, None, None)
