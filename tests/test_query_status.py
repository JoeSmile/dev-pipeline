"""WorkflowEnvironment tests for DP-27 Query observability and status CLI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    start_parent,
    story,
    wait_snapshot,
    wait_until,
)

from dev_pipeline.cli_status import format_status, query_pipeline_status
from dev_pipeline.progress import UNRESPONSIVE_LABEL, waiting_confirmation
from dev_pipeline.progress_log import load_recorded_progress
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


async def test_query_history_fields_match_stage_waiting_budget_and_mirror(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            snap = await wait_snapshot(
                handle,
                lambda item: item.stage is ParentStage.INTAKE and item.budget is not None,
            )
            assert snap.waiting_signal == "lock_scope"
            assert snap.progress is not None
            assert snap.progress.waiting_confirmation == "正在等候你确认锁定范围"
            assert snap.budget is not None
            comments_before = controller.jira.list_comments(payload.story_id)
            again = await handle.query(DevPipelineWorkflow.snapshot)
            assert again.stage is snap.stage
            assert again.waiting_signal == snap.waiting_signal
            assert again.budget is not None
            assert again.last_failure_reason == snap.last_failure_reason
            assert controller.jira.list_comments(payload.story_id) == comments_before
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-query-fields"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_in_flight_step_updates_while_stage_stays_implementing(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    controller.hold("implement", "T1")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            await advance_to_implementing(handle, payload)
            snap = await wait_snapshot(
                handle,
                lambda item: item.stage is ParentStage.IMPLEMENTING and item.tickets,
            )
            await wait_snapshot(
                handle,
                lambda item: any(ticket.started for ticket in item.tickets),
            )
            assert snap.stage is ParentStage.IMPLEMENTING
            await wait_until(
                lambda: "正在写代码：实现T1"
                in load_recorded_progress(str(tmp_config_root), payload.execution_id)[
                    "recent_hints"
                ]
            )
            recorded = load_recorded_progress(str(tmp_config_root), payload.execution_id)
            assert "正在写代码：实现T1" in recorded["recent_hints"]
            assert "正在收集 diff 与测试证据" in recorded["recent_hints"]
            assert recorded["current_step"]
            assert recorded["last_heartbeat_at"]
            status = await query_pipeline_status(handle, config_root=str(tmp_config_root))
            assert status.stage is ParentStage.IMPLEMENTING
            assert status.progress is not None
            assert status.progress.current_step
            assert len(status.progress.recent_hints) >= 2
            assert status.progress.unresponsive_label is None
            first_beat = status.progress.last_heartbeat_at
            status_again = await query_pipeline_status(handle, config_root=str(tmp_config_root))
            assert status_again.stage is ParentStage.IMPLEMENTING
            assert status_again.progress is not None
            assert status_again.progress.last_heartbeat_at
            assert first_beat is not None
            controller.release("implement", "T1")
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-inflight"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_stale_heartbeat_is_unresponsive_not_failed_or_waiting(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    controller.hold("implement", "T1")
    controller.pause_heartbeat.add("T1")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            await advance_to_implementing(handle, payload)
            await wait_snapshot(
                handle,
                lambda item: item.stage is ParentStage.IMPLEMENTING and any(
                    ticket.started for ticket in item.tickets
                ),
            )
            await asyncio.sleep(1.2)
            status = await query_pipeline_status(
                handle,
                config_root=str(tmp_config_root),
                expiry=timedelta(seconds=1),
            )
            assert status.stage is ParentStage.IMPLEMENTING
            assert status.stage is not ParentStage.BLOCKED
            assert status.tickets[0].stage is not TicketStage.FAILED
            assert status.progress is not None
            assert status.progress.unresponsive_label == UNRESPONSIVE_LABEL
            assert status.progress.waiting_confirmation is None
            assert status.waiting_signal != "lock_scope"
            controller.release("implement", "T1")
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-expired"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_human_gate_wait_is_confirmation_not_unresponsive(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            snap = await handle.query(DevPipelineWorkflow.snapshot)
            assert waiting_confirmation(snap.waiting_signal) == "正在等候你确认锁定范围"
            assert snap.progress is not None
            assert snap.progress.waiting_confirmation == "正在等候你确认锁定范围"
            assert snap.progress.unresponsive_label is None
            # A stale clock must not relabel a human wait as unresponsive.
            stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            status = await query_pipeline_status(
                handle,
                config_root=str(tmp_config_root),
                recorded={"current_step": "", "last_heartbeat_at": stale, "recent_hints": []},
                expiry=timedelta(seconds=1),
            )
            assert status.progress is not None
            assert status.progress.waiting_confirmation == "正在等候你确认锁定范围"
            assert status.progress.unresponsive_label is None
            assert status.stage is ParentStage.INTAKE
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-waiting"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_query_has_no_side_effects_on_stage_jira_or_files(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            snap = await handle.query(DevPipelineWorkflow.snapshot)
            comments = [item.body for item in controller.jira.list_comments(payload.story_id)]
            files_before = {
                path: path.stat().st_mtime
                for path in tmp_config_root.rglob("*")
                if path.is_file()
            }
            await query_pipeline_status(handle, config_root=str(tmp_config_root))
            again = await handle.query(DevPipelineWorkflow.snapshot)
            assert again.stage is snap.stage
            assert again.waiting_signal == snap.waiting_signal
            bodies = [item.body for item in controller.jira.list_comments(payload.story_id)]
            assert bodies == comments
            files_after = {
                path: path.stat().st_mtime
                for path in tmp_config_root.rglob("*")
                if path.is_file()
            }
            assert files_after == files_before
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-side-effects"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_history_and_jira_are_not_flooded_with_steps_or_secrets(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            await advance_to_implementing(handle, payload)
            await wait_snapshot(
                handle,
                lambda item: item.stage in {ParentStage.IMPLEMENTING, ParentStage.BLOCKED}
                or item.tickets,
            )
            history = await handle.fetch_history()
            blob = str(history)
            assert "CURSOR_API_KEY" not in blob
            assert "完整思考" not in blob
            for comment in controller.jira.list_comments(payload.story_id):
                assert "正在写代码" not in comment.body
                assert "正在收集 diff" not in comment.body
            assert "abort" not in signaled_names(history)
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-history"})
        finally:
            await worker.__aexit__(None, None, None)


async def test_status_formatter_prints_query_fields_without_follow(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            snap = await query_pipeline_status(handle, config_root=str(tmp_config_root))
            text = format_status(snap)
            assert "stage=Intake" in text
            assert "waiting_signal=lock_scope" in text
            assert "正在等候你确认锁定范围" in text
            assert "budget_spent=" in text
            assert "jira_mirror_lagged=" in text
            assert "current_step=" in text
            assert "last_heartbeat_at=" in text
            assert "unresponsive_label=" in text
            assert "recent_hints=" in text
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-format"})
        finally:
            await worker.__aexit__(None, None, None)


