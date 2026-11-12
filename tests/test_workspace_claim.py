"""Unit tests for the workspace / side-effect claim keyed by ticket_id + execution_id."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from dev_pipeline.activities.stubs import implement_ticket
from dev_pipeline.activities.workspace import claim_ticket_workspace
from dev_pipeline.models import TicketWorkInput
from dev_pipeline.workspace import (
    MAINLINE_BRANCH,
    WorkspaceClaimStore,
    build_claim,
    claim_workspace,
    default_claim_store,
    derive_branch_name,
    reset_default_claim_store,
)


def _work(ticket_id: str, execution_id: str, config_root: Path | None = None) -> TicketWorkInput:
    return TicketWorkInput(
        ticket_id=ticket_id,
        execution_id=execution_id,
        story_id="STORY",
        config_root="" if config_root is None else str(config_root),
    )


def test_second_claim_returns_first_and_does_not_increment_created_count() -> None:
    store = WorkspaceClaimStore()
    first = store.claim("A", "exec-1")
    second = store.claim("A", "exec-1")
    assert second == first
    assert store.created_count("A", "exec-1") == 1
    assert store.total_created() == 1
    assert first.workspace_id == "ws/A/exec-1"
    assert first.branch_name == "ticket/A/exec-1"
    assert first.pr_token == "pr-A-exec-1"


def test_different_execution_id_gets_a_new_workspace_identity() -> None:
    store = WorkspaceClaimStore()
    first = store.claim("A", "exec-1")
    second = store.claim("A", "exec-2")
    assert second.workspace_id != first.workspace_id
    assert second.branch_name != first.branch_name
    assert second.pr_token != first.pr_token
    assert "exec-2" in second.workspace_id
    assert "exec-2" in second.branch_name
    assert store.created_count("A", "exec-1") == 1
    assert store.created_count("A", "exec-2") == 1
    assert store.get("A", "exec-1") == first


def test_claim_identities_never_point_at_mainline() -> None:
    claim = build_claim("A", "exec-1")
    assert claim.branch_name != MAINLINE_BRANCH
    assert derive_branch_name("A", "exec-1") != MAINLINE_BRANCH
    assert MAINLINE_BRANCH not in (claim.workspace_id, claim.pr_token)


def test_commit_failure_does_not_mutate_sibling_or_leave_partial_claim() -> None:
    store = WorkspaceClaimStore()
    sibling = store.claim("B", "exec-1")

    def boom(key: str, record: object) -> None:
        raise RuntimeError("claim failed")

    store._commit = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="claim failed"):
        store.claim("A", "exec-1")
    assert store.get("B", "exec-1") == sibling
    assert store.get("A", "exec-1") is None
    assert store.created_count("A", "exec-1") == 0
    assert store.created_count("B", "exec-1") == 1


@pytest.mark.asyncio
async def test_production_stubs_reuse_the_same_claim() -> None:
    reset_default_claim_store()
    try:
        env = ActivityEnvironment()
        payload = _work("A", "exec-1")
        first = await env.run(claim_ticket_workspace, payload)
        second = await env.run(claim_ticket_workspace, payload)
        await env.run(implement_ticket, payload)
        store = default_claim_store()
        assert first == second
        assert store.created_count("A", "exec-1") == 1
        assert store.get("A", "exec-1") == first
        assert claim_workspace(payload) == first
    finally:
        reset_default_claim_store()


def test_authorized_worktree_is_reused_for_the_same_key(tmp_path: Path) -> None:
    from tests.sandbox_fixtures import write_workspace_toml

    from dev_pipeline.workspace_auth import resolve_workspace_claim

    config = tmp_path / "config"
    config.mkdir()
    root = tmp_path / "authorized"
    write_workspace_toml(config, root)
    store = WorkspaceClaimStore()
    first = resolve_workspace_claim(_work("T1", "exec-1", config), store=store)
    second = resolve_workspace_claim(_work("T1", "exec-1", config), store=store)
    other = resolve_workspace_claim(_work("T1", "exec-2", config), store=store)
    assert first.authorized_worktree == str(root / "T1" / "exec-1")
    assert second.authorized_worktree == first.authorized_worktree
    assert store.created_count("T1", "exec-1") == 1
    assert other.authorized_worktree == str(root / "T1" / "exec-2")
    assert other.authorized_worktree != first.authorized_worktree
    assert (root / "T1" / "exec-1").is_dir()
    assert (root / "T1" / "exec-2").is_dir()


def test_unsafe_ticket_id_is_rejected(tmp_path: Path) -> None:
    from tests.sandbox_fixtures import write_workspace_toml

    from dev_pipeline.errors import WorkspaceConfigError
    from dev_pipeline.workspace_auth import resolve_workspace_claim

    config = tmp_path / "config"
    config.mkdir()
    write_workspace_toml(config, tmp_path / "authorized")
    store = WorkspaceClaimStore()
    with pytest.raises(WorkspaceConfigError, match="路径分隔"):
        resolve_workspace_claim(_work("..", "exec-1", config), store=store)
