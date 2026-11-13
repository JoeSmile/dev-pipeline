"""Unit tests for DP-30 abort recycle and read-only inheritance builders."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.sandbox_fixtures import write_workspace_toml

from dev_pipeline.abort_record import AbortCleanupInput, AbortWorkspaceTarget
from dev_pipeline.inheritance import build_inherited_artifacts
from dev_pipeline.models import (
    PENDING_FIELD,
    SchemeDecision,
    SchemeHypothesis,
    ScopeLock,
    TicketSnapshot,
)
from dev_pipeline.remote_pr import RecordingPrClient, reset_default_pr_client
from dev_pipeline.stages import TicketStage
from dev_pipeline.verify import RESULT_HELD, VerifyEvidenceLine, VerifyRecord
from dev_pipeline.workspace import derive_branch_name
from dev_pipeline.worktree_recycle import (
    recycle_aborted_execution,
    recycle_hook_detail,
    worktree_recycle_hook,
)

_GIT_TIMEOUT = 30


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "NUL" if os.name == "nt" else "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = env["GIT_CONFIG_GLOBAL"]
    env["GIT_TERMINAL_PROMPT"] = "0"
    git = shutil.which("git")
    assert git is not None
    return subprocess.run(
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=True,
        env=env,
    )


def _init_repo(path: Path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(path, "-c", "user.name=dev-pipeline", "-c", "user.email=dev-pipeline@local",
         "commit", "--allow-empty", "-m", "initial")
    _git(path, "checkout", "-b", branch)
    (path / "scratch.txt").write_text("work", encoding="utf-8")


def _payload(
    config_root: Path,
    execution_id: str,
    workspaces: list[AbortWorkspaceTarget],
) -> AbortCleanupInput:
    return AbortCleanupInput(
        story_id="STORY-1",
        execution_id=execution_id,
        config_root=str(config_root),
        reason="stop",
        aborted_at="2026-11-12T12:00:00Z",
        workspaces=workspaces,
    )


def test_recycle_hook_still_does_not_delete(tmp_path: Path) -> None:
    worktree = tmp_path / "tree"
    worktree.mkdir()
    marker = worktree / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    marker_text = worktree_recycle_hook(str(worktree), ticket_id="T1", execution_id="exec-1")
    assert marker_text == PENDING_FIELD
    assert "待证" in recycle_hook_detail()
    assert marker.is_file()


def test_abort_recycles_this_execution_tree_and_branch(tmp_path: Path) -> None:
    config = tmp_path / "config"
    authorized = tmp_path / "authorized"
    config.mkdir()
    write_workspace_toml(config, authorized)
    execution_id = "exec-1"
    other_id = "exec-other"
    this_tree = authorized / "T1" / execution_id
    other_tree = authorized / "T1" / other_id
    branch = derive_branch_name("T1", execution_id)
    _init_repo(this_tree, branch)
    other_tree.mkdir(parents=True, exist_ok=True)
    (other_tree / "sibling.txt").write_text("keep-sibling", encoding="utf-8")
    (authorized / "root-marker.txt").write_text("keep-root", encoding="utf-8")

    record = recycle_aborted_execution(
        _payload(
            config,
            execution_id,
            [
                AbortWorkspaceTarget(
                    ticket_id="T1",
                    execution_id=execution_id,
                    worktree=str(this_tree),
                    branch_name=branch,
                )
            ],
        )
    )
    assert record.history_preserved is True
    assert record.reason == "stop"
    assert "可回放" in record.detail
    assert not this_tree.exists()
    assert other_tree.is_dir()
    assert (other_tree / "sibling.txt").read_text(encoding="utf-8") == "keep-sibling"
    assert (authorized / "root-marker.txt").is_file()
    assert authorized.is_dir()
    recycled = [item for item in record.recycled if item.ticket_id == "T1"]
    assert recycled
    assert recycled[0].worktree_removed is True


def test_abort_recycles_linked_worktree_inside_authorized_root(tmp_path: Path) -> None:
    config = tmp_path / "config"
    authorized = tmp_path / "authorized"
    config.mkdir()
    write_workspace_toml(config, authorized)
    main = authorized / "_main"
    _init_repo(main, "scratch")
    _git(main, "checkout", "main")
    execution_id = "exec-2"
    branch = derive_branch_name("T2", execution_id)
    worktree = authorized / "T2" / execution_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(main, "worktree", "add", "-b", branch, str(worktree))
    assert worktree.is_dir()
    record = recycle_aborted_execution(
        _payload(
            config,
            execution_id,
            [
                AbortWorkspaceTarget(
                    ticket_id="T2",
                    execution_id=execution_id,
                    worktree=str(worktree),
                    branch_name=branch,
                )
            ],
        )
    )
    assert not worktree.exists()
    branches = _git(main, "branch", "--list", branch).stdout
    assert branch not in branches
    assert main.is_dir()
    assert any(item.worktree_removed for item in record.recycled)


def test_existing_remote_pr_is_not_closed_or_merged(tmp_path: Path) -> None:
    reset_default_pr_client()
    config = tmp_path / "config"
    authorized = tmp_path / "authorized"
    config.mkdir()
    write_workspace_toml(config, authorized)
    execution_id = "exec-3"
    branch = derive_branch_name("T3", execution_id)
    worktree = authorized / "T3" / execution_id
    _init_repo(worktree, branch)
    from dev_pipeline.remote_pr import RemotePrRef

    client = RecordingPrClient()
    pr_ref = "https://example.invalid/pr/3"
    client.open_prs = [RemotePrRef(branch_name=branch, pr_ref=pr_ref, ticket_id="T3")]
    record = recycle_aborted_execution(
        _payload(
            config,
            execution_id,
            [
                AbortWorkspaceTarget(
                    ticket_id="T3",
                    execution_id=execution_id,
                    worktree=str(worktree),
                    branch_name=branch,
                    remote_pr_url=pr_ref,
                    remote_pr_opened=True,
                )
            ],
        ),
        pr_client=client,
    )
    assert client.close_calls == []
    assert client.merge_calls == []
    assert record.remote_prs_pending_human
    pending = record.remote_prs_pending_human[0]
    assert pending.pr_ref == pr_ref
    assert pending.closed is False
    assert pending.merged is False
    assert pending.status == "待人工处理"
    assert pending.marking_method == PENDING_FIELD
    assert not worktree.exists()
    reset_default_pr_client()


def test_symlink_escape_is_not_followed(tmp_path: Path) -> None:
    config = tmp_path / "config"
    authorized = tmp_path / "authorized"
    outside = tmp_path / "outside"
    config.mkdir()
    write_workspace_toml(config, authorized)
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("do-not-delete", encoding="utf-8")
    execution_id = "exec-4"
    link = authorized / "T4" / execution_id
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    record = recycle_aborted_execution(
        _payload(
            config,
            execution_id,
            [
                AbortWorkspaceTarget(
                    ticket_id="T4",
                    execution_id=execution_id,
                    worktree=str(link),
                    branch_name=derive_branch_name("T4", execution_id),
                )
            ],
        )
    )
    assert secret.read_text(encoding="utf-8") == "do-not-delete"
    assert outside.is_dir()
    assert not link.exists() or not link.is_symlink()
    assert record.recycled


def test_external_git_repo_is_not_mutated(tmp_path: Path) -> None:
    config = tmp_path / "config"
    authorized = tmp_path / "authorized"
    config.mkdir()
    write_workspace_toml(config, authorized)
    outside = tmp_path / "external-repo"
    _init_repo(outside, "scratch")
    _git(outside, "checkout", "main")
    execution_id = "exec-5"
    branch = derive_branch_name("T5", execution_id)
    worktree = authorized / "T5" / execution_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(outside, "worktree", "add", "-b", branch, str(worktree))
    record = recycle_aborted_execution(
        _payload(
            config,
            execution_id,
            [
                AbortWorkspaceTarget(
                    ticket_id="T5",
                    execution_id=execution_id,
                    worktree=str(worktree),
                    branch_name=branch,
                )
            ],
        )
    )
    assert worktree.exists()
    branches = _git(outside, "branch", "--list", branch).stdout
    assert branch in branches
    assert any("授权根之外" in item.skipped_reason for item in record.recycled)


def test_inheritance_builder_copies_allowed_fields_only() -> None:
    scope = ScopeLock(
        in_scope="登录",
        out_of_scope="计费",
        locked_by="郭伟",
        locked_at="2026-11-12T12:00:00Z",
    )
    scheme = SchemeDecision(
        tradeoff="单点登录",
        hypotheses=[SchemeHypothesis(statement="局部可替换", grade="局部")],
        decided_by="郭伟",
        decided_at="2026-11-12T12:01:00Z",
        retrieval="桩",
    )
    verified = TicketSnapshot(
        ticket_id="A",
        stage=TicketStage.VERIFIED,
        verify=VerifyRecord(
            ticket_id="A",
            execution_id="exec-old",
            passed=True,
            evidence_path="evidence/verify.md",
            reason="过线证据齐",
            lines=[
                VerifyEvidenceLine(
                    red_line="红线",
                    command_or_phenomenon="pytest",
                    result=RESULT_HELD,
                    evidence_path="evidence/verify.md",
                )
            ],
        ),
    )
    failed = TicketSnapshot(ticket_id="B", stage=TicketStage.FAILED, detail="no")
    inherited = build_inherited_artifacts(
        scope=scope,
        scheme=scheme,
        tickets=[verified, failed],
    )
    assert inherited.scope is not None
    assert inherited.scope.in_scope == "登录"
    assert inherited.scheme is not None
    assert inherited.scheme.tradeoff == "单点登录"
    assert inherited.hypotheses[0].statement == "局部可替换"
    assert [item.ticket_id for item in inherited.closed_ticket_evidence] == ["A"]
    assert inherited.closed_ticket_evidence[0].evidence_path == "evidence/verify.md"
    assert failed.ticket_id not in [item.ticket_id for item in inherited.closed_ticket_evidence]
