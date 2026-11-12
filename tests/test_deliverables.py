"""Unit tests for deterministic ticket deliverables."""

from __future__ import annotations

import os
from pathlib import Path

from dev_pipeline.coding_cli_adapter import plan_coding_cli
from dev_pipeline.deliverables import produce_implement_result, stub_ticket_deliverables
from dev_pipeline.models import (
    PENDING_FIELD,
    TicketDeliverables,
    TicketWorkInput,
    is_complete_deliverables,
    missing_deliverable_fields,
    ticket_workflow_id,
)
from dev_pipeline.sandbox import remote_git_action_reason
from dev_pipeline.workspace import derive_branch_name
from dev_pipeline.worktree_recycle import recycle_hook_detail, worktree_recycle_hook


def _payload(
    tmp_path: Path, ticket_id: str = "T1", execution_id: str = "exec-1"
) -> TicketWorkInput:
    return TicketWorkInput(
        ticket_id=ticket_id,
        execution_id=execution_id,
        story_id="STORY-1",
        config_root=str(tmp_path / "config"),
    )


def _worktree(tmp_path: Path, ticket_id: str = "T1", execution_id: str = "exec-1") -> Path:
    path = tmp_path / "authorized" / ticket_id / execution_id
    path.mkdir(parents=True)
    return path


def test_completeness_requires_four_fields() -> None:
    empty = TicketDeliverables()
    assert is_complete_deliverables(None) is False
    assert is_complete_deliverables(empty) is False
    assert missing_deliverable_fields(empty) == ["branch", "diff", "tests", "rollback"]
    complete = TicketDeliverables(
        branch_name="ticket/T1/exec-1",
        diff="diff --git a/ticket_impl.py",
        test_evidence="cwd=/tmp/wt\nexit=0",
        rollback_notes="回滚说明",
    )
    assert is_complete_deliverables(complete) is True
    assert missing_deliverable_fields(complete) == []


def test_produce_writes_branch_diff_tests_and_rollback(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    payload = _payload(tmp_path)
    result = produce_implement_result(payload, str(worktree))
    record = result.deliverables
    assert result.diff_submitted is True
    assert record is not None
    assert record.complete is True
    assert record.branch_name == derive_branch_name("T1", "exec-1")
    assert "ticket_impl.py" in record.diff
    assert "ROLLBACK.md" in record.diff or "rollback" in record.diff.lower()
    assert str(worktree.resolve()) in record.test_evidence
    assert record.tests_ran_in_worktree is True
    assert "回滚" in record.rollback_notes
    assert record.pushed is False
    assert record.merged is False
    assert record.remote_pr_opened is False
    assert record.remote_pr_url == ""
    assert record.pr_authorization == PENDING_FIELD
    assert record.worktree_recycle == PENDING_FIELD
    assert record.backend == "桩"
    assert (worktree / "ticket_impl.py").is_file()
    assert (worktree / "tests" / "test_ticket_impl.py").is_file()
    assert (worktree / "ROLLBACK.md").is_file()


def test_tests_run_inside_authorized_worktree(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    result = produce_implement_result(_payload(tmp_path), str(worktree))
    assert result.deliverables is not None
    evidence = result.deliverables.test_evidence
    assert f"cwd={worktree.resolve()}" in evidence
    assert "exit=0" in evidence
    assert result.deliverables.tests_ran_in_worktree is True


def test_same_key_rerun_reuses_branch_and_does_not_open_pr(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    payload = _payload(tmp_path)
    first = produce_implement_result(payload, str(worktree))
    sha = _git_sha(worktree)
    second = produce_implement_result(payload, str(worktree))
    assert first.deliverables is not None and second.deliverables is not None
    assert first.deliverables.branch_name == second.deliverables.branch_name
    assert second.deliverables.remote_pr_opened is False
    assert second.deliverables.pushed is False
    assert _git_sha(worktree) == sha
    branches = _git_stdout(worktree, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    ticket_branches = [line for line in branches.splitlines() if line.startswith("ticket/")]
    assert ticket_branches == [first.deliverables.branch_name]


def test_unauthorized_worktree_is_incomplete_and_does_not_push(tmp_path: Path) -> None:
    result = produce_implement_result(_payload(tmp_path), "")
    assert result.diff_submitted is False
    assert result.deliverables is not None
    assert result.deliverables.complete is False
    assert "branch" in result.deliverables.missing
    assert result.deliverables.pushed is False
    assert result.deliverables.merged is False
    assert result.deliverables.remote_pr_opened is False
    assert "未授权" in result.detail


def test_git_push_and_merge_remain_forbidden(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    produce_implement_result(_payload(tmp_path), str(worktree))
    remotes = _git_stdout(worktree, "remote")
    assert remotes.strip() == ""
    assert remote_git_action_reason(["git", "push", "origin", "HEAD"]) is not None
    assert remote_git_action_reason(["git", "merge", "main"]) is not None


def test_recycle_hook_is_pending_and_does_not_delete(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    produce_implement_result(_payload(tmp_path), str(worktree))
    marker = worktree_recycle_hook(str(worktree), ticket_id="T1", execution_id="exec-1")
    assert marker == PENDING_FIELD
    assert "待证" in recycle_hook_detail()
    assert worktree.is_dir()
    assert (worktree / "ticket_impl.py").is_file()


def test_reserved_coding_cli_adapter_is_unwired(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    plan = plan_coding_cli(str(worktree))
    assert plan.wired is False
    assert plan.invoked is False
    assert plan.argv[:5] == ["agent", "-p", "--force", "--trust", "--workspace"]
    assert "--worktree" not in plan.argv
    assert "--api-key" not in plan.argv
    key_file = tmp_path / "config" / "CURSOR_API_KEY"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("super-secret-key", encoding="utf-8")
    produce_implement_result(_payload(tmp_path), str(worktree))
    # The deliverable backend must not read the key or copy it into artifacts.
    rollback = (worktree / "ROLLBACK.md").read_text(encoding="utf-8")
    diff = produce_implement_result(_payload(tmp_path), str(worktree)).deliverables
    assert diff is not None
    assert "super-secret-key" not in rollback
    assert "super-secret-key" not in diff.diff
    assert "super-secret-key" not in diff.test_evidence


def test_stub_deliverables_follow_the_same_key(tmp_path: Path) -> None:
    complete = stub_ticket_deliverables("T9", "exec-9")
    assert complete.complete is True
    assert complete.branch_name == derive_branch_name("T9", "exec-9")
    assert ticket_workflow_id("T9", "exec-9") == "T9+exec-9"
    incomplete = stub_ticket_deliverables("T9", "exec-9", complete=False)
    assert incomplete.complete is False
    assert is_complete_deliverables(incomplete) is False


def _git_sha(worktree: Path) -> str:
    return _git_stdout(worktree, "rev-parse", "HEAD").strip()


def _git_stdout(worktree: Path, *args: str) -> str:
    import subprocess

    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    proc = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return proc.stdout
