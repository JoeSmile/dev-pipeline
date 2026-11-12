"""WorkflowEnvironment tests for dual review, divergence, and the 2000-line gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names, scheduled_before_any_complete
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.test_deliverables_workflow import _wait_child

from dev_pipeline.activities.review import review_ticket
from dev_pipeline.activities.sandbox import apply_sandbox_write
from dev_pipeline.activities.stubs import implement_ticket
from dev_pipeline.activities.workspace import claim_ticket_workspace
from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.deliverables import collect_reviewable_diff, stub_ticket_deliverables
from dev_pipeline.models import (
    ImplementResult,
    ReviewFindings,
    SandboxWriteSpec,
    TicketInput,
    TicketWorkInput,
    ticket_workflow_id,
)
from dev_pipeline.review import MAX_FILE_LINES
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows import TicketWorkflow
from dev_pipeline.workspace import derive_branch_name, reset_default_claim_store
from dev_pipeline.workspace_auth import resolve_workspace_claim

pytestmark = pytest.mark.asyncio


def _authorized_config(tmp_config_root: Path) -> Path:
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    return authorized


async def _finish(handle: object, worker: Worker) -> None:
    try:
        desc = await handle.describe()  # type: ignore[union-attr]
        if desc.status not in {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.FAILED,
        }:
            await handle.terminate()  # type: ignore[union-attr]
    except Exception:
        pass
    await worker.__aexit__(None, None, None)


@activity.defn(name="implement_ticket")
async def planted_oversized_implement(payload: TicketWorkInput) -> ImplementResult:
    """Write a 2001-line file into the claimed worktree so review can block it."""

    claim = resolve_workspace_claim(payload)
    worktree = Path(claim.authorized_worktree)
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "huge.py").write_text(
        "\n".join(f"line-{i}" for i in range(MAX_FILE_LINES + 1)),
        encoding="utf-8",
    )
    _init_ticket_repo(
        worktree,
        derive_branch_name(payload.ticket_id, payload.execution_id),
        extra=["huge.py"],
    )
    diff = collect_reviewable_diff(claim.authorized_worktree)
    deliverables = stub_ticket_deliverables(payload.ticket_id, payload.execution_id)
    deliverables.diff = diff or "planted-huge"
    deliverables.worktree = claim.authorized_worktree
    deliverables.complete = True
    return ImplementResult(diff_submitted=True, detail="planted", deliverables=deliverables)


def _activities_real_review(controller: StubController) -> list[object]:
    kept: list[object] = []
    for item in controller.activities():
        name = getattr(item, "__name__", "")
        if name in {
            "implement_ticket",
            "claim_ticket_workspace",
            "apply_sandbox_write",
            "review_ticket",
        }:
            continue
        kept.append(item)
    kept.extend([claim_ticket_workspace, apply_sandbox_write, implement_ticket, review_ticket])
    return kept


async def _run_child(
    env: WorkflowEnvironment,
    payload: TicketInput,
    activities: list[object],
) -> tuple[object, Worker]:
    task_queue = f"{DEFAULT_TASK_QUEUE}-rev-{payload.execution_id}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=activities,
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    return handle, worker


async def test_dual_reviews_run_in_parallel_and_advice_goes_to_verifying(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    try:
        _authorized_config(tmp_config_root)
        controller = StubController()
        payload = TicketInput(
            ticket_id="T1",
            execution_id="exec-advice",
            story_id="STORY-rev",
            config_root=str(tmp_config_root),
            parent_workflow_id="",
        )
        handle, worker = await _run_child(
            workflow_env, payload, _activities_real_review(controller)
        )
        try:
            snap = await _wait_child(
                handle,
                stages=frozenset({TicketStage.VERIFYING, TicketStage.VERIFIED}),
            )
            assert snap.stage in {TicketStage.VERIFYING, TicketStage.VERIFIED}
            assert snap.review is not None
            assert len(snap.review.workers) == 2
            assert {report.worker_id for report in snap.review.workers} == {"standards", "spec"}
            assert snap.review.destination == "Verifying"
            assert snap.review.requires_human_gate is False
            assert snap.review.rounds == 1
            assert snap.review.no_new_evidence is True
            assert "无新证据" in snap.review.stopped_reason
            assert all(item.rule_citation for item in snap.review.findings)
            assert all(item.severity in {"建议", "信息"} for item in snap.review.findings)
            history = await handle.fetch_history()
            names = scheduled_activity_names(history)
            assert names.count("review_ticket") == 2
            assert scheduled_before_any_complete(history, "review_ticket") == 2
            # No second review loop and no costly model/debate path.
            assert names.count("review_ticket") == 2
            assert "run_debate" not in names
            assert "run_model_call" not in names
            assert "run_rag" not in names
        finally:
            await _finish(handle, worker)
    finally:
        reset_default_claim_store()


async def test_blocking_or_paradigm_drift_waits_at_human_gate(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.review_findings["T-block"] = ReviewFindings(blocking=["must gate"])
    payload = TicketInput(
        ticket_id="T-block",
        execution_id="exec-block",
        story_id="STORY-block",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run_child(workflow_env, payload, list(controller.activities()))
    try:
        snap = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
        assert snap.stage is TicketStage.HUMAN_GATE
        assert snap.review is not None
        assert snap.review.requires_human_gate is True
        assert snap.review.destination == "HumanGate"
        assert snap.review.auto_arbitrated is False
        await asyncio.sleep(0.2)
        still = await handle.query(TicketWorkflow.snapshot)
        assert still.stage is TicketStage.HUMAN_GATE
        history = await handle.fetch_history()
        assert scheduled_activity_names(history).count("review_ticket") == 2
    finally:
        await _finish(handle, worker)


async def test_paradigm_drift_enters_human_gate(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.review_findings["T-drift"] = ReviewFindings(info=["ok"], paradigm_drift=True)
    payload = TicketInput(
        ticket_id="T-drift",
        execution_id="exec-drift",
        story_id="STORY-drift",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run_child(workflow_env, payload, list(controller.activities()))
    try:
        snap = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
        assert snap.stage is TicketStage.HUMAN_GATE
        assert snap.review is not None
        assert snap.review.paradigm_drift is True
        assert snap.review.auto_arbitrated is False
    finally:
        await _finish(handle, worker)


async def test_conflicting_blocks_are_not_auto_arbitrated(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.review_findings_by_worker["T-conflict"] = {
        "standards": ReviewFindings(blocking=["L0 被改"]),
        "spec": ReviewFindings(blocking=["不得新增 forbidden.py"]),
    }
    payload = TicketInput(
        ticket_id="T-conflict",
        execution_id="exec-conflict",
        story_id="STORY-conflict",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run_child(workflow_env, payload, list(controller.activities()))
    try:
        snap = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
        assert snap.stage is TicketStage.HUMAN_GATE
        assert snap.review is not None
        assert snap.review.conflicting_blocks is True
        assert snap.review.auto_arbitrated is False
        assert snap.review.destination == "HumanGate"
        assert len(snap.review.conflict_reasons) == 2
        await asyncio.sleep(0.2)
        still = await handle.query(TicketWorkflow.snapshot)
        assert still.stage is TicketStage.HUMAN_GATE
    finally:
        await _finish(handle, worker)


async def test_oversized_file_is_hard_block(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    try:
        _authorized_config(tmp_config_root)
        controller = StubController()
        kept: list[object] = []
        for item in controller.activities():
            name = getattr(item, "__name__", "")
            if name in {"implement_ticket", "review_ticket"}:
                continue
            kept.append(item)
        kept.extend([planted_oversized_implement, review_ticket])
        payload = TicketInput(
            ticket_id="T-huge",
            execution_id="exec-huge",
            story_id="STORY-huge",
            config_root=str(tmp_config_root),
            parent_workflow_id="",
        )
        handle, worker = await _run_child(workflow_env, payload, kept)
        try:
            snap = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
            assert snap.stage is TicketStage.HUMAN_GATE
            assert snap.review is not None
            assert any(
                item.severity == "阻断" and "2000" in (item.summary + item.rule_citation)
                for item in snap.review.findings
            )
            assert all(
                item.severity == "阻断"
                for item in snap.review.findings
                if "2000" in (item.summary + item.rule_citation)
            )
        finally:
            await _finish(handle, worker)
    finally:
        reset_default_claim_store()


async def test_review_write_is_rejected_on_the_snapshot(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    try:
        authorized = _authorized_config(tmp_config_root)
        controller = StubController()
        payload = TicketInput(
            ticket_id="T-ro",
            execution_id="exec-ro",
            story_id="STORY-ro",
            config_root=str(tmp_config_root),
            parent_workflow_id="",
            review_write=SandboxWriteSpec(path="hacked.txt", content="nope"),
        )
        handle, worker = await _run_child(
            workflow_env, payload, _activities_real_review(controller)
        )
        try:
            snap = await _wait_child(
                handle,
                stages=frozenset(
                    {TicketStage.HUMAN_GATE, TicketStage.VERIFYING, TicketStage.VERIFIED}
                ),
            )
            assert snap.review is not None
            assert snap.review.write_rejected is True
            assert "只读" in snap.review.write_reason
            assert snap.review.workspace_unchanged is True
            assert snap.stage is TicketStage.HUMAN_GATE
            claimed = authorized / "T-ro" / "exec-ro"
            assert (claimed / "hacked.txt").exists() is False
        finally:
            await _finish(handle, worker)
    finally:
        reset_default_claim_store()


async def test_stub_advice_still_reaches_verifying(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    payload = TicketInput(
        ticket_id="T-stub",
        execution_id="exec-stub",
        story_id="STORY-stub",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    handle, worker = await _run_child(workflow_env, payload, list(controller.activities()))
    try:
        snap = await _wait_child(
            handle, stages=frozenset({TicketStage.VERIFYING, TicketStage.VERIFIED})
        )
        assert snap.stage in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert snap.review is not None
        assert snap.review.destination == "Verifying"
        assert scheduled_activity_names(await handle.fetch_history()).count("review_ticket") == 2
    finally:
        await _finish(handle, worker)


def _init_ticket_repo(worktree: Path, branch: str, extra: list[str] | None = None) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    git("config", "user.name", "dev-pipeline")
    git("config", "user.email", "dev-pipeline@local")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    git("commit", "--allow-empty", "-m", "initial")
    git("checkout", "-b", branch)
    for rel in extra or []:
        git("add", "--", rel)
    if extra:
        git("commit", "-m", "plant")
