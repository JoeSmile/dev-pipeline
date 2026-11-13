"""Recycle unmerged worktrees and local branches created by one execution.

The deliverable-time hook remains non-mutating. Real cleanup runs from the
abort Activity. Workflow modules must not import this file.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from dev_pipeline.abort_record import (
    ABORT_HISTORY_PRESERVED,
    AbortCleanupInput,
    AbortRecord,
    AbortWorkspaceTarget,
    RecycledWorkspace,
    RemotePrHumanPending,
)
from dev_pipeline.models import PENDING_FIELD
from dev_pipeline.remote_pr import (
    RecordingPrClient,
    RemotePrRef,
    default_pr_client,
    pending_from_ref,
)
from dev_pipeline.sandbox import assert_no_auto_push_or_merge, is_within_authorized
from dev_pipeline.workspace import derive_branch_name
from dev_pipeline.workspace_config import load_authorized_root_from_path

RECYCLE_PENDING = PENDING_FIELD
_HOOK_DETAIL = (
    "交付时不回收工作区。abort 后由 Activity 回收本 execution 创建的未合并 "
    "worktree / 本地分支；已有远程 PR 不关闭也不合并，只记录待人工处理（标记方式待证）。"
)
_GIT_TIMEOUT = 30
_SKIP_OUTSIDE = "工作区不在本 execution 的授权根内，已跳过回收"
_SKIP_ROOT = "拒绝回收授权根目录本身"
_SKIP_EXTERNAL_GIT = "工作区指向授权根之外的 git 仓库，已跳过回收，待人工处理"
_ABORT_DETAIL = (
    "已留下可回放记录；本 execution 创建的未合并工作区与本地分支已回收；"
    "已有远程 PR 未关闭也未合并，待人工处理（标记方式待证）。"
)
_PR_CLOSE_MERGE_MARKERS = (
    "pr close",
    "pr merge",
    "pulls/",
    "pull request",
    "gh pr close",
    "gh pr merge",
)


def worktree_recycle_hook(worktree: str, *, ticket_id: str, execution_id: str) -> str:
    """Return the pending marker. Does not remove or leak a worktree.

    Actual recycle happens later on abort, not at deliverable production.

    Args:
        worktree: Authorized worktree path created for this execution.
        ticket_id: Implementation ticket id.
        execution_id: Execution id that created the tree.

    Returns:
        The pending marker ``待证``. No filesystem mutation is performed.
    """

    del worktree, ticket_id, execution_id
    return RECYCLE_PENDING


def recycle_hook_detail() -> str:
    """Return the operator-visible explanation of the recycle hook."""

    return _HOOK_DETAIL


def recycle_aborted_execution(
    payload: AbortCleanupInput,
    *,
    pr_client: RecordingPrClient | None = None,
) -> AbortRecord:
    """Recycle this execution's workspaces and record any remote PRs for humans.

    Other execution_id trees, the authorized root, and git repositories
    outside that root are left untouched. History is never deleted.
    Close or merge APIs are not called.
    """

    client = pr_client if pr_client is not None else default_pr_client()
    root = ""
    if payload.config_root.strip():
        root = load_authorized_root_from_path(payload.config_root)
    targets = _collect_targets(payload, root)
    pending_prs = _pending_remote_prs(targets, client)
    recycled = [_recycle_one(item, root) for item in targets]
    return AbortRecord(
        reason=payload.reason,
        aborted_at=payload.aborted_at,
        execution_id=payload.execution_id,
        recycled=recycled,
        remote_prs_pending_human=pending_prs,
        history_preserved=True,
        detail=f"{_ABORT_DETAIL} {ABORT_HISTORY_PRESERVED}",
    )


def _collect_targets(
    payload: AbortCleanupInput,
    authorized_root: str,
) -> list[AbortWorkspaceTarget]:
    merged: dict[str, AbortWorkspaceTarget] = {}
    for item in payload.workspaces:
        if item.execution_id and item.execution_id != payload.execution_id:
            continue
        key = item.worktree or f"{item.ticket_id}:{item.branch_name}"
        merged[key] = AbortWorkspaceTarget(
            ticket_id=item.ticket_id,
            execution_id=payload.execution_id,
            worktree=item.worktree,
            branch_name=item.branch_name
            or derive_branch_name(item.ticket_id, payload.execution_id),
            remote_pr_url=item.remote_pr_url,
            remote_pr_opened=item.remote_pr_opened,
        )
    if not authorized_root:
        return list(merged.values())
    for path in _scan_execution_workspaces(authorized_root, payload.execution_id):
        ticket_id = path.parent.name
        key = str(path)
        if key in merged:
            continue
        merged[key] = AbortWorkspaceTarget(
            ticket_id=ticket_id,
            execution_id=payload.execution_id,
            worktree=key,
            branch_name=derive_branch_name(ticket_id, payload.execution_id),
        )
    return list(merged.values())


def _scan_execution_workspaces(authorized_root: str, execution_id: str) -> list[Path]:
    root = Path(authorized_root)
    found: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        candidate = Path(entry.path) / execution_id
        if _safe_existing_under(candidate, root):
            found.append(candidate)
    return found


def _pending_remote_prs(
    targets: list[AbortWorkspaceTarget],
    client: RecordingPrClient,
) -> list[RemotePrHumanPending]:
    branches = [item.branch_name for item in targets if item.branch_name]
    detected = {item.pr_ref: item for item in client.find_open_prs(branches)}
    pending: list[RemotePrHumanPending] = []
    seen: set[str] = set()
    for item in targets:
        refs: list[RemotePrRef] = []
        if item.remote_pr_opened or item.remote_pr_url.strip():
            refs.append(
                RemotePrRef(
                    branch_name=item.branch_name,
                    pr_ref=item.remote_pr_url.strip() or item.branch_name,
                    ticket_id=item.ticket_id,
                )
            )
        for found in detected.values():
            if found.branch_name == item.branch_name:
                refs.append(
                    RemotePrRef(
                        branch_name=found.branch_name,
                        pr_ref=found.pr_ref,
                        ticket_id=found.ticket_id or item.ticket_id,
                    )
                )
        for pr in refs:
            if pr.pr_ref in seen:
                continue
            seen.add(pr.pr_ref)
            pending.append(pending_from_ref(pr))
    return pending


def _recycle_one(item: AbortWorkspaceTarget, authorized_root: str) -> RecycledWorkspace:
    record = RecycledWorkspace(
        ticket_id=item.ticket_id,
        execution_id=item.execution_id,
        worktree=item.worktree,
        branch_name=item.branch_name,
    )
    if not item.worktree.strip():
        return record
    path = Path(item.worktree)
    if not authorized_root:
        record.skipped_reason = _SKIP_OUTSIDE
        return record
    root = Path(authorized_root)
    if _is_authorized_root(path, root):
        record.skipped_reason = _SKIP_ROOT
        return record
    if not _lexical_under(path, root):
        record.skipped_reason = _SKIP_OUTSIDE
        return record
    if path.is_symlink():
        return _recycle_symlink(path, root, record)
    if not path.exists():
        record.worktree_removed = True
        record.branch_deleted = True
        return record
    external = _external_git_common_dir(path, root)
    if external is not None:
        record.skipped_reason = _SKIP_EXTERNAL_GIT
        return record
    linked = _is_linked_worktree(path)
    repo = _git_common_dir(path)
    record.worktree_removed = _remove_worktree_dir(path, root)
    if linked:
        target = repo if repo is not None else path
        record.branch_deleted = _delete_local_branch(target, item.branch_name, root)
    elif record.worktree_removed and not path.exists():
        record.branch_deleted = True
    else:
        record.branch_deleted = _delete_local_branch(path, item.branch_name, root)
    return record


def _recycle_symlink(path: Path, root: Path, record: RecycledWorkspace) -> RecycledWorkspace:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if not is_within_authorized(str(resolved), str(root)):
        try:
            path.unlink()
            record.worktree_removed = True
        except OSError as exc:
            record.skipped_reason = f"无法删除越出授权根的符号链接: {exc}"
        return record
    try:
        path.unlink()
        record.worktree_removed = True
        record.branch_deleted = True
    except OSError as exc:
        record.skipped_reason = f"无法删除符号链接工作区: {exc}"
    return record


def _delete_local_branch(repo_or_tree: Path, branch: str, root: Path) -> bool:
    if not branch or branch in {"main", "master"}:
        return False
    repo = _git_common_dir(repo_or_tree)
    if repo is None:
        repo = repo_or_tree
    try:
        repo_ok = is_within_authorized(str(repo.resolve()), str(root))
    except OSError:
        repo_ok = is_within_authorized(str(repo), str(root))
    if not repo_ok:
        return False
    cwd = str(repo if repo.is_dir() else repo_or_tree)
    current = _git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if current == branch:
        _git_ok(cwd, "checkout", "--detach") or _git_ok(cwd, "checkout", "main")
    if not _git_ok(cwd, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        return True
    return _git_ok(cwd, "branch", "-D", branch)


def _remove_worktree_dir(path: Path, root: Path) -> bool:
    if path.is_dir() and not path.is_symlink() and _is_linked_worktree(path):
        repo = _git_common_dir(path)
        if repo is not None and is_within_authorized(str(repo), str(root)):
            _git_ok(str(repo), "worktree", "remove", "--force", str(path))
    if path.exists():
        _safe_rmtree(path, root)
    return not path.exists()


def _is_linked_worktree(path: Path) -> bool:
    git = path / ".git"
    try:
        return git.is_file()
    except OSError:
        return False


def _git_common_dir(path: Path) -> Path | None:
    git = path / ".git"
    try:
        if git.is_dir():
            return path
        if git.is_file():
            raw = git.read_text(encoding="utf-8").strip()
            prefix = "gitdir:"
            if raw.lower().startswith(prefix):
                gitdir = Path(raw[len(prefix) :].strip())
                if not gitdir.is_absolute():
                    gitdir = (path / gitdir).resolve()
                return _main_worktree_from_gitdir(gitdir)
    except OSError:
        return None
    return None


def _main_worktree_from_gitdir(gitdir: Path) -> Path:
    # Linked worktrees use <main>/.git/worktrees/<name>.
    if gitdir.name and gitdir.parent.name == "worktrees":
        return gitdir.parent.parent.parent
    if gitdir.name == ".git":
        return gitdir.parent
    return gitdir


def _external_git_common_dir(path: Path, root: Path) -> Path | None:
    repo = _git_common_dir(path)
    if repo is None:
        return None
    try:
        resolved = repo.resolve()
    except OSError:
        resolved = repo
    if is_within_authorized(str(resolved), str(root)):
        return None
    return resolved


def _safe_existing_under(path: Path, root: Path) -> bool:
    try:
        if not path.exists():
            return False
    except OSError:
        return False
    if not _lexical_under(path, root):
        return False
    if path.is_symlink():
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return is_within_authorized(str(resolved), str(root))
    return is_within_authorized(str(path.resolve()), str(root))


def _lexical_under(path: Path, root: Path) -> bool:
    return is_within_authorized(str(path), str(root))


def _is_authorized_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve() == root.resolve()
    except OSError:
        return str(path) == str(root)


def _safe_rmtree(path: Path, root: Path) -> None:
    """Remove *path* without following symlinks out of the authorized tree."""

    if not _lexical_under(path, root):
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
    except OSError:
        return
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for entry in entries:
        child = Path(entry.path)
        try:
            if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                _clear_readonly(child)
                child.unlink()
            elif entry.is_dir(follow_symlinks=False):
                _safe_rmtree(child, root)
        except OSError:
            continue
    try:
        _clear_readonly(path)
        path.rmdir()
    except OSError:
        try:
            shutil.rmtree(path, onexc=_rmtree_onexc)
        except OSError:
            pass


def _clear_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def _rmtree_onexc(func: object, path: str, exc: BaseException) -> None:
    del func, exc
    _clear_readonly(Path(path))
    try:
        Path(path).unlink()
    except OSError:
        pass


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    null = "NUL" if os.name == "nt" else "/dev/null"
    env["GIT_CONFIG_GLOBAL"] = null
    env["GIT_CONFIG_SYSTEM"] = null
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("CURSOR_API_KEY", None)
    return env


def _git_bin() -> str | None:
    return shutil.which("git")


def _git_run(cwd: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    git = _git_bin()
    if git is None:
        return None
    argv = [git, *args]
    assert_no_auto_push_or_merge(argv)
    _assert_no_pr_close_or_merge(argv)
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_ok(cwd: str, *args: str) -> bool:
    result = _git_run(cwd, *args)
    return result is not None and result.returncode == 0


def _git_output(cwd: str, *args: str) -> str:
    result = _git_run(cwd, *args)
    if result is None or result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _assert_no_pr_close_or_merge(argv: list[str]) -> None:
    lowered = " ".join(argv).lower()
    for marker in _PR_CLOSE_MERGE_MARKERS:
        if marker in lowered:
            raise RuntimeError(f"abort 回收不得关闭或合并远程 PR: {argv}")
