"""Assert human signals and reopen start appear in Temporal History."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import start_parent, story, wait_snapshot, wait_terminal

from dev_pipeline.models import (
    AbortRequest,
    ApproveSpikeRequest,
    PromoteRuleRequest,
    ReopenRequest,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    TicketSpec,
)
from dev_pipeline.signal_schemas import HUMAN_SIGNAL_NAMES
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


async def test_history_contains_human_signals_and_reopen_start(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            SchemeDecision(has_high_risk_assumption=False, tickets=list(payload.tickets)),
        )
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="A", decision="approve"),
        )
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            PromoteRuleRequest(rule_name="skill", automatic=False),
        )
        await handle.signal(DevPipelineWorkflow.authorize_test_deploy)
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id="too-early"))
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
        )
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="history-abort"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-from-history"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        assert snap.stage is ParentStage.BLOCKED
        await handle.result()
        history = await handle.fetch_history()
        names = signaled_names(history)
        missing = [name for name in HUMAN_SIGNAL_NAMES if name not in names]
        assert missing == [], f"History 缺少 Signal: {missing}; 实际={names}"
        assert "start_reopened_workflow" in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)
