"""WorkflowEnvironment tests for Story Retro, Done, and promote_rule."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    promote_rule_request,
    stage_of,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.activities.retro import apply_promote_rule, write_retro_record
from dev_pipeline.models import AbortRequest
from dev_pipeline.retro import (
    CANDIDATE_STATUS,
    REASON_AUTO_PROMOTE,
    REASON_INELIGIBLE,
    CandidateRecord,
    is_difficulty_score_report,
    render_candidate_text,
)
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


def _real_retro_activities(controller: StubController) -> list[object]:
    skip = {"write_retro_record", "apply_promote_rule"}
    kept = [item for item in controller.activities() if getattr(item, "__name__", "") not in skip]
    kept.extend([write_retro_record, apply_promote_rule])
    return kept


async def _start_with_real_retro(
    env: WorkflowEnvironment,
    tmp_config_root: Path,
    tickets: list[object] | None = None,
    *,
    controller: StubController | None = None,
):
    ctrl = controller or StubController()
    payload = story(tmp_config_root, list(tickets or [complete_ticket("A")]))
    import uuid

    from temporalio.worker import Worker

    from dev_pipeline.activities.reopen import ReopenStarter
    from dev_pipeline.constants import DEFAULT_TASK_QUEUE
    from dev_pipeline.models import story_workflow_id
    from dev_pipeline.workflows import TicketWorkflow

    task_queue = f"{DEFAULT_TASK_QUEUE}-retro-{uuid.uuid4().hex[:8]}"
    starter = ReopenStarter(env.client)
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*_real_retro_activities(ctrl), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload, ctrl


async def _finish(handle: object, worker: object) -> None:
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
    await worker.__aexit__(None, None, None)  # type: ignore[union-attr]


def _daily_files(config_root: Path) -> list[Path]:
    folder = config_root / "docs" / "knowledge" / "daily"
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.md"))


async def test_verified_story_writes_retro_and_reaches_done(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    worker, handle, payload, _ctrl = await _start_with_real_retro(workflow_env, tmp_config_root)
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.retro is not None
        assert snap.retro.daily_path
        daily = Path(snap.retro.daily_path)
        assert daily.is_file()
        text = daily.read_text(encoding="utf-8")
        assert payload.story_id in text
        assert is_difficulty_score_report(text) is False
        assert "技术难度" not in text
        assert "业务难度" not in text
        assert all(item.status == CANDIDATE_STATUS for item in snap.retro.candidates)
        history = await handle.fetch_history()
        assert "write_retro_record" in scheduled_activity_names(history)
    finally:
        await _finish(handle, worker)


async def test_unverified_ticket_cannot_enter_retro_or_done(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle, payload, _ctrl = await _start_with_real_retro(
        workflow_env, tmp_config_root, controller=controller
    )
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(handle, lambda item: started(item, "A"))
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage not in {ParentStage.RETRO, ParentStage.DONE}
        assert snap.retro is None
        assert _daily_files(tmp_config_root) == []
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-unverified"))
        await wait_terminal(handle)
    finally:
        await _finish(handle, worker)


async def test_verify_failure_does_not_enter_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.verify_passed["A"] = False
    worker, handle, payload, _ctrl = await _start_with_real_retro(
        workflow_env, tmp_config_root, controller=controller
    )
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.IMPLEMENTING
        )
        # After a failed verify the child returns to Implementing; parent stays put.
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage not in {ParentStage.RETRO, ParentStage.DONE}
        assert snap.retro is None
        assert _daily_files(tmp_config_root) == []
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-verify-fail"))
        await wait_terminal(handle)
    finally:
        await _finish(handle, worker)


async def test_append_existing_daily_and_reject_auto_promote(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    from datetime import UTC, datetime, timedelta

    from dev_pipeline.retro import daily_note_date

    now = datetime.now(UTC)
    daily_dir = tmp_config_root / "docs" / "knowledge" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    # Seed nearby calendar days so append is covered whether the Workflow
    # clock is wall time (start_local) or a time-skipping instant.
    for delta in (-1, 0, 1):
        day = daily_note_date(now + timedelta(days=delta))
        (daily_dir / f"{day}.md").write_text(
            f"# {day} 日记\n\n## 先前的 Story\n\n- 保留这段。\n",
            encoding="utf-8",
        )
    worker, handle, payload, _ctrl = await _start_with_real_retro(
        workflow_env, tmp_config_root, tickets=[complete_ticket("A")]
    )
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        await wait_terminal(handle)
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.retro is not None
        text = Path(snap.retro.daily_path).read_text(encoding="utf-8")
        assert "先前的 Story" in text
        assert "保留这段" in text
        assert payload.story_id in text
        assert all(item.status == CANDIDATE_STATUS for item in snap.retro.candidates)
    finally:
        await _finish(handle, worker)


async def test_promote_rule_legal_and_illegal_paths(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    cand_dir = tmp_config_root / "docs" / "knowledge" / "candidates"
    cand_dir.mkdir(parents=True)
    eligible = CandidateRecord(
        candidate_id="cand-ok",
        title="可晋升",
        hits=2,
        citations=["evidence/verify.md"],
    )
    weak = CandidateRecord(candidate_id="cand-weak", title="不足", hits=1)
    (cand_dir / "cand-ok.md").write_text(render_candidate_text(eligible), encoding="utf-8")
    (cand_dir / "cand-weak.md").write_text(render_candidate_text(weak), encoding="utf-8")

    controller = StubController()
    controller.hold("implement", "A")
    worker, handle, payload, _ctrl = await _start_with_real_retro(
        workflow_env, tmp_config_root, controller=controller
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            promote_rule_request("cand-ok", automatic=True),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.promote_rule_status is not None and "拒绝" in (item.promote_rule_status or "")
            ),
        )
        assert REASON_AUTO_PROMOTE.split("，")[0] in (snap.promote_rule_status or "") or "拒绝" in (
            snap.promote_rule_status or ""
        )
        assert snap.promotion is None
        assert "状态: candidate" in (cand_dir / "cand-ok.md").read_text(encoding="utf-8")

        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            promote_rule_request("cand-weak", reason="想跳过门槛"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "promote_rule"
                and REASON_INELIGIBLE in item.last_rejection.reason
            ),
        )
        assert snap.promotion is None

        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            {"candidate_id": "cand-ok", "target": "L0"},
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and "缺少必填字段" in item.last_rejection.reason
            ),
        )
        assert snap.promotion is None

        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            promote_rule_request("cand-ok", target="L0", evidence_citation="evidence/verify.md"),
        )
        snap = await wait_snapshot(handle, lambda item: item.promotion is not None)
        assert snap.promotion is not None
        assert snap.promotion.candidate_id == "cand-ok"
        assert snap.promotion.target == "L0"
        assert "已晋升为L0" in (snap.promote_rule_status or "")
        body = (cand_dir / "cand-ok.md").read_text(encoding="utf-8")
        assert "状态: L0" in body
        history = await handle.fetch_history()
        assert "apply_promote_rule" in scheduled_activity_names(history)

        controller.release("implement", "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await _finish(handle, worker)
