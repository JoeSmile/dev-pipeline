"""WorkflowEnvironment tests for abort recycle and reopen inheritance (DP-30)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    start_parent,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.models import AbortRequest, ReopenRequest, TicketDeliverables, story_workflow_id
from dev_pipeline.remote_pr import reset_default_pr_client
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow
from dev_pipeline.workspace import derive_branch_name, reset_default_claim_store
from dev_pipeline.workspace_auth import derive_authorized_worktree

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    """Real local test server for cross-execution reopen behavior."""
    cli = os.environ.get("TEMPORAL_DEV_SERVER_PATH", "").strip() or None
    kwargs = {"ui": False}
    if cli:
        kwargs["dev_server_existing_path"] = cli
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        yield env


def _authorized_config(tmp_config_root: Path) -> Path:
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    return authorized


async def test_abort_recycles_this_execution_workspace_and_keeps_history(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    reset_default_pr_client()
    authorized = _authorized_config(tmp_config_root)
    sibling = authorized / "A" / "exec-sibling"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "keep.txt").write_text("sibling", encoding="utf-8")
    payload = story(tmp_config_root, [complete_ticket("A")])
    this_tree = Path(derive_authorized_worktree(str(authorized), "A", payload.execution_id))
    controller = StubController()
    worker, handle = await start_parent(
        local_env,
        payload,
        controller,
    )
    # Replace activities by restarting is heavy; use the default stub claim
    # which still creates the authorized directory once children start.
    try:
        await advance_to_implementing(handle, payload)
        await wait_until(lambda: this_tree.is_dir())
        assert this_tree.is_dir()
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="回收本执行工作区"))
        snap = await wait_terminal(handle)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.abort_record is not None
        assert snap.abort_record.reason == "回收本执行工作区"
        assert snap.abort_record.history_preserved is True
        assert "可回放" in snap.abort_record.detail
        assert not this_tree.exists()
        assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling"
        history = await handle.fetch_history()
        assert "recycle_aborted_execution" in scheduled_activity_names(history)
        assert history.events
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
        reset_default_pr_client()


async def test_abort_records_existing_pr_without_close_or_merge(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    reset_default_pr_client()
    authorized = _authorized_config(tmp_config_root)
    payload = story(tmp_config_root, [complete_ticket("A")])
    branch = derive_branch_name("A", payload.execution_id)
    pr_ref = "https://example.invalid/pr/30"
    controller = StubController()
    controller.implement_deliverables["A"] = TicketDeliverables(
        branch_name=branch,
        diff="stub-diff",
        test_evidence="stub-tests",
        rollback_notes="stub-rollback",
        complete=True,
        tests_ran_in_worktree=True,
        worktree=str(authorized / "A" / payload.execution_id),
        remote_pr_opened=True,
        remote_pr_url=pr_ref,
    )
    from dev_pipeline.remote_pr import RemotePrRef, default_pr_client

    default_pr_client().open_prs.append(
        RemotePrRef(branch_name=branch, pr_ref=pr_ref, ticket_id="A")
    )
    worker, handle = await start_parent(local_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: any(ticket.started for ticket in item.tickets))
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-with-pr"))
        snap = await wait_terminal(handle)
        assert snap.abort_record is not None
        pending = snap.abort_record.remote_prs_pending_human
        assert pending
        assert pending[0].pr_ref == pr_ref
        assert pending[0].closed is False
        assert pending[0].merged is False
        assert pending[0].marking_method == "待证"
        client = default_pr_client()
        assert client.close_calls == []
        assert client.merge_calls == []
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
        reset_default_pr_client()


async def test_reopen_inherits_readonly_artifacts_and_uses_new_workspace(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = _authorized_config(tmp_config_root)
    payload = story(tmp_config_root, [complete_ticket("A")])
    old_tree = Path(derive_authorized_worktree(str(authorized), "A", payload.execution_id))
    worker, handle = await start_parent(local_env, payload, StubController())
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: any(ticket.stage is TicketStage.VERIFIED for ticket in item.tickets),
        )
        locked_scope = snap.scope
        locked_scheme = snap.scheme
        assert locked_scope is not None
        assert locked_scheme is not None
        assert old_tree.is_dir()
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-after-verify"))
        await wait_terminal(handle)
        assert not old_tree.exists()
        new_id = f"{payload.execution_id}-reopen"
        new_tree = Path(derive_authorized_worktree(str(authorized), "A", new_id))
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        old_snap = await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        assert old_snap.execution_id == payload.execution_id
        new_handle = local_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        new_snap = await wait_snapshot(
            new_handle,
            lambda item: item.inherited is not None and item.scheme is not None,
        )
        assert new_snap.execution_id == new_id
        assert new_snap.scope is not None
        assert new_snap.scope.in_scope == locked_scope.in_scope
        assert new_snap.scope.locked_at == locked_scope.locked_at
        assert new_snap.scheme is not None
        assert new_snap.scheme.tradeoff == locked_scheme.tradeoff
        assert [item.statement for item in new_snap.scheme.hypotheses] == [
            item.statement for item in locked_scheme.hypotheses
        ]
        assert new_snap.inherited is not None
        assert [item.ticket_id for item in new_snap.inherited.closed_ticket_evidence] == ["A"]
        assert new_snap.inherited.closed_ticket_evidence[0].verify is not None
        assert new_tree != old_tree
        await new_handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-reopened"))
        await wait_terminal(new_handle)
        assert not old_tree.exists()
        history = await handle.fetch_history()
        assert history.events
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_reopen_from_done_does_not_write_old_workspace(
    local_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = _authorized_config(tmp_config_root)
    payload = story(tmp_config_root, [complete_ticket("A")])
    old_tree = Path(derive_authorized_worktree(str(authorized), "A", payload.execution_id))
    worker, handle = await start_parent(local_env, payload, StubController())
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        snap = await wait_terminal(handle)
        assert snap.stage is ParentStage.DONE
        assert old_tree.is_dir()
        marker = old_tree / "old-execution.txt"
        marker.write_text("do-not-touch", encoding="utf-8")
        new_id = f"{payload.execution_id}-reopen-done"
        new_tree = Path(derive_authorized_worktree(str(authorized), "A", new_id))
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        new_handle = local_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        new_snap = await wait_snapshot(
            new_handle,
            lambda item: item.inherited is not None and item.scheme is not None,
        )
        assert new_snap.execution_id == new_id
        assert marker.read_text(encoding="utf-8") == "do-not-touch"
        assert new_tree != old_tree
        # The old execution must not start writing into its previous tree again.
        assert new_snap.execution_id != payload.execution_id
        await new_handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-new"))
        await wait_terminal(new_handle)
        assert marker.read_text(encoding="utf-8") == "do-not-touch"
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
