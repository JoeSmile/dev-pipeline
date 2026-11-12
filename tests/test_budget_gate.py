"""Workflow tests for the parent budget gate (DP-12)."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    exception_text,
    stage_of,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.budget import BudgetAmounts, CostlyPathRequest, CostlyPathResult
from dev_pipeline.models import AbortRequest, TicketSpec
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


def _write_budget(root: Path, text: str) -> None:
    (root / "budget.toml").write_text(text, encoding="utf-8")


async def _wait_budget(handle: object) -> object:
    return await wait_snapshot(handle, lambda snap: snap.budget is not None)


async def test_snapshot_exposes_configurable_spent_limits_and_remaining(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_budget(
        tmp_config_root,
        # 待证占位，仅用于验证限额来自配置而非代码写死。
        'units = ["tokens", "calls"]\n[limits]\ntokens = 40\ncalls = 8\n',
    )
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _wait_budget(handle)
        assert snap.budget is not None
        assert snap.budget.units == ["tokens", "calls"]
        assert snap.budget.limits == BudgetAmounts(tokens=40, calls=8, cost=0)
        assert snap.budget.spent == BudgetAmounts()
        assert snap.budget.remaining == BudgetAmounts(tokens=40, calls=8, cost=0)
        assert snap.budget.last_breach is None
        first = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="model"),
        )
        assert first.started is True
        assert first.status == "completed"
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        assert snap.budget.remaining.calls == 7
        assert snap.budget.remaining.tokens == 40
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_breach_stops_further_model_and_retrieval_starts(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_budget(tmp_config_root, 'units = ["calls"]\n[limits]\ncalls = 1\n')
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _wait_budget(handle)
        first = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="rag"),
        )
        assert first.started is True
        assert controller.costly_started == ["rag"]
        second = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="model"),
        )
        assert second.started is False
        assert second.status == "refused"
        assert "Bad" in second.reason
        assert "calls" in second.reason
        assert "待证" in second.reason
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        assert snap.budget.remaining.calls == 0
        assert snap.budget.last_breach is not None
        assert snap.budget.last_breach.bad == "Bad"
        assert snap.budget.last_breach.path == "rag"
        assert snap.budget.last_breach.unit == "calls"
        assert "不会继续计费" in snap.budget.last_breach.reason
        assert set(snap.budget.stopped_paths) == {"debate", "rag", "model"}
        third = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="rag"),
        )
        assert third.started is False
        assert controller.costly_started == ["rag"]
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert names.count("run_rag") == 1
        assert "run_model_call" not in names
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_no_new_evidence_stops_only_that_path(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_budget(tmp_config_root, 'units = ["calls"]\n[limits]\ncalls = 10\n')
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    controller.queue_costly(
        "debate",
        CostlyPathResult(
            path="debate",
            has_new_evidence=False,
            usage=BudgetAmounts(calls=1),
            detail="test-no-evidence",
        ),
    )
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _wait_budget(handle)
        first = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="debate"),
        )
        assert first.started is True and first.status == "no_new_evidence"
        assert "无新证据" in first.reason
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1 and snap.budget.last_breach is None
        assert "debate" in snap.budget.stopped_paths
        assert "debate" in snap.budget.no_new_evidence_paths
        assert "rag" not in snap.budget.stopped_paths
        refused = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="debate"),
        )
        rag = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="rag"),
        )
        assert refused.started is False and rag.started is True
        assert controller.costly_started == ["debate", "rag"]
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert names.count("run_debate") == 1
        assert names.count("run_rag") == 1
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_budget_breach_does_not_cancel_sibling_tickets(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_budget(tmp_config_root, 'units = ["tokens"]\n[limits]\ntokens = 5\n')
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    controller.queue_costly(
        "model",
        CostlyPathResult(
            path="model",
            has_new_evidence=True,
            usage=BudgetAmounts(tokens=5),
            detail="test-over-limit",
        ),
    )
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.IMPLEMENTING
                and stage_of(item, "B") is TicketStage.IMPLEMENTING
            ),
        )
        outcome = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="model"),
        )
        assert outcome.started is True
        refused = await handle.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="rag"),
        )
        assert refused.started is False
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.stage is ParentStage.IMPLEMENTING
        assert started(snap, "A") and started(snap, "B")
        stages = {item.ticket_id: item.stage for item in snap.tickets}
        assert stages["A"] is TicketStage.IMPLEMENTING
        assert stages["B"] is TicketStage.IMPLEMENTING
        assert snap.budget is not None
        assert snap.budget.last_breach is not None
        assert snap.budget.last_breach.bad == "Bad"
        controller.release("implement", "A", "B")
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-after-breach"))
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.BLOCKED
        assert result.blocked_reason == "stop-after-breach"
    finally:
        await worker.__aexit__(None, None, None)


async def test_missing_budget_file_fails_workflow_visibly(
    workflow_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    empty = tmp_path / "no-budget"
    empty.mkdir()
    payload = story(empty, [TicketSpec(ticket_id="A")])
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        text = exception_text(exc.value)
        assert "缺少预算配置文件" in text
        assert "不会使用隐藏默认限额" in text
    finally:
        await worker.__aexit__(None, None, None)


async def test_limits_are_taken_from_config_not_hard_coded(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-root"
    other.mkdir()
    _write_budget(tmp_config_root, 'units = ["calls"]\n[limits]\ncalls = 0\n')
    _write_budget(other, 'units = ["calls"]\n[limits]\ncalls = 2\n')
    tight = story(tmp_config_root, [TicketSpec(ticket_id="A")], suffix="tight")
    loose = story(other, [TicketSpec(ticket_id="A")], suffix="loose")
    controller = StubController()
    worker, first = await start_parent(workflow_env, tight, controller)
    try:
        await _wait_budget(first)
        refused = await first.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="debate"),
        )
        assert refused.started is False
        assert "Bad" in refused.reason
        await first.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-tight"))
        await wait_terminal(first)
    finally:
        await worker.__aexit__(None, None, None)

    worker, second = await start_parent(workflow_env, loose, controller, queue="q-loose")
    try:
        await _wait_budget(second)
        allowed = await second.execute_update(
            DevPipelineWorkflow.start_costly_path,
            CostlyPathRequest(path="debate"),
        )
        assert allowed.started is True
        snap = await second.query(DevPipelineWorkflow.snapshot)
        assert snap.budget is not None
        assert snap.budget.limits.calls == 2
        await second.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-loose"))
        await wait_terminal(second)
    finally:
        await worker.__aexit__(None, None, None)
