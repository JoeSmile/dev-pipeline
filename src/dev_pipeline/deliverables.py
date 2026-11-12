"""Deterministic ticket deliverables: branch, diff, unit tests, rollback notes.

This backend writes only inside an already authorized worktree and never
calls Cursor CLI or an LLM. Default is no remote push, merge, or PR.
Workflow modules must not import this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dev_pipeline.errors import SandboxViolation
from dev_pipeline.models import (
    PENDING_FIELD,
    ImplementResult,
    TicketDeliverables,
    TicketWorkInput,
    is_complete_deliverables,
    missing_deliverable_fields,
)
from dev_pipeline.sandbox import (
    apply_guarded_write,
    assert_no_auto_push_or_merge,
    is_within_authorized,
)
from dev_pipeline.workspace import derive_branch_name
from dev_pipeline.worktree_recycle import worktree_recycle_hook

DELIVERABLE_BACKEND_STUB = "桩"
IMPL_PATH = "ticket_impl.py"
TEST_PATH = "tests/test_ticket_impl.py"
ROLLBACK_PATH = "ROLLBACK.md"
BASE_BRANCH = "main"
_GIT_TIMEOUT = 30
_TEST_TIMEOUT = 30
_UNAUTH = "未授权工作区，无法在授权 worktree 内交付分支、diff、单测与回滚说明"
_NO_GIT = "授权 worktree 内找不到 git，无法交付分支与 diff"
_GIT_FAILED = "在授权 worktree 内执行 git 失败: {detail}"
_TESTS_NOT_IN_TREE = "单测未在该 Ticket 的授权 worktree 内运行"


def stub_ticket_deliverables(
    ticket_id: str,
    execution_id: str,
    *,
    complete: bool = True,
) -> TicketDeliverables:
    """Return a stub deliverable record for WorkflowEnvironment test doubles."""

    branch = derive_branch_name(ticket_id, execution_id)
    if complete:
        record = TicketDeliverables(
            branch_name=branch,
            diff="stub-diff",
            test_evidence="stub-tests: ran in authorized worktree (桩)",
            rollback_notes="stub-rollback",
            tests_ran_in_worktree=True,
            backend=DELIVERABLE_BACKEND_STUB,
            pr_authorization=PENDING_FIELD,
            worktree_recycle=PENDING_FIELD,
        )
        record.complete = True
        return record
    record = TicketDeliverables(
        branch_name=branch,
        backend=DELIVERABLE_BACKEND_STUB,
        pr_authorization=PENDING_FIELD,
        worktree_recycle=PENDING_FIELD,
    )
    record.missing = missing_deliverable_fields(record)
    record.complete = False
    return record


def produce_implement_result(
    payload: TicketWorkInput,
    authorized_worktree: str,
) -> ImplementResult:
    """Create or reuse local deliverables inside the authorized worktree.

    Same-key reentry reuses the derived branch and does not open a PR.
    Unauthorized or incomplete work does not enter Reviewing.

    Args:
        payload: Ticket identity and config-root. Contains no secrets.
        authorized_worktree: Absolute authorized path, or empty if unauthorized.

    Returns:
        An implement result whose ``diff_submitted`` is True only when all
        four deliverables are complete.
    """

    branch = derive_branch_name(payload.ticket_id, payload.execution_id)
    record = TicketDeliverables(
        worktree=authorized_worktree,
        backend=DELIVERABLE_BACKEND_STUB,
        pr_authorization=PENDING_FIELD,
        worktree_recycle=worktree_recycle_hook(
            authorized_worktree,
            ticket_id=payload.ticket_id,
            execution_id=payload.execution_id,
        ),
    )
    if not authorized_worktree.strip():
        record.missing = missing_deliverable_fields(record)
        return ImplementResult(
            diff_submitted=False,
            detail=_UNAUTH,
            deliverables=record,
        )
    try:
        _ensure_repo_and_branch(authorized_worktree, branch)
        record.branch_name = branch
        if not _has_deliverable_files(authorized_worktree):
            _write_deliverable_files(
                authorized_worktree,
                payload.ticket_id,
                payload.execution_id,
                branch,
            )
            _commit_local(authorized_worktree, branch)
        record.diff = _collect_diff(authorized_worktree)
        record.rollback_notes = _read_rollback(authorized_worktree)
        evidence, ran = _run_tests_in_worktree(authorized_worktree)
        record.test_evidence = evidence
        record.tests_ran_in_worktree = ran
        if not ran and "授权 worktree" not in record.test_evidence:
            record.test_evidence = (
                record.test_evidence + "\n" + _TESTS_NOT_IN_TREE
            ).strip()
        record.complete = (
            is_complete_deliverables(record) and record.tests_ran_in_worktree
        )
        record.missing = missing_deliverable_fields(record)
        if not record.tests_ran_in_worktree and "tests" not in record.missing:
            record.missing.append("tests")
        detail = (
            "deliverables-complete"
            if record.complete
            else "交付物不齐，不能进入 Reviewing"
        )
        return ImplementResult(
            diff_submitted=record.complete,
            detail=detail,
            deliverables=record,
        )
    except SandboxViolation as exc:
        record.missing = missing_deliverable_fields(record)
        return ImplementResult(
            diff_submitted=False,
            detail=exc.reason or str(exc),
            deliverables=record,
        )


def collect_reviewable_diff(worktree: str) -> str:
    """Return the reviewable local diff inside an authorized worktree."""

    if not worktree.strip():
        return ""
    try:
        return _collect_diff(worktree)
    except SandboxViolation:
        return ""


def _has_deliverable_files(worktree: str) -> bool:
    root = Path(worktree)
    return all((root / path).is_file() for path in (IMPL_PATH, TEST_PATH, ROLLBACK_PATH))


def _write_deliverable_files(
    worktree: str,
    ticket_id: str,
    execution_id: str,
    branch: str,
) -> None:
    impl = (
        '"""Minimal deterministic implementation for one ticket execution."""\n'
        "\n"
        "\n"
        "def ticket_value() -> int:\n"
        "    return 1\n"
    )
    test = (
        "import sys\n"
        "import unittest\n"
        "from pathlib import Path\n"
        "\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "\n"
        "import ticket_impl\n"
        "\n"
        "\n"
        "class TicketImplTests(unittest.TestCase):\n"
        "    def test_value(self) -> None:\n"
        "        self.assertEqual(ticket_impl.ticket_value(), 1)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    unittest.main()\n"
    )
    rollback = (
        f"# 回滚说明\n\n"
        f"- Ticket: `{ticket_id}`\n"
        f"- execution: `{execution_id}`\n"
        f"- 分支: `{branch}`\n\n"
        "本 execution 默认未向远程 push，也未合并，也未开远程 PR。\n"
        "回滚：在授权 worktree 内将工作区切回 "
        f"`{BASE_BRANCH}`，并删除本地分支 `{branch}`。\n"
        "未合并 worktree 的回收见 DP-30（待证）。\n"
        "开远程 PR 的授权条件见 DP-31（待证），默认视为未授权。\n"
    )
    for path, content in (
        (IMPL_PATH, impl),
        (TEST_PATH, test),
        (ROLLBACK_PATH, rollback),
    ):
        written = apply_guarded_write(worktree, path, content)
        if not written.allowed:
            reason = written.record.reason or "写入交付物被沙箱拒绝"
            raise SandboxViolation(reason, path=path, authorized_worktree=worktree)


def _read_rollback(worktree: str) -> str:
    path = Path(worktree) / ROLLBACK_PATH
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    null = "NUL" if os.name == "nt" else "/dev/null"
    env["GIT_CONFIG_GLOBAL"] = null
    env["GIT_CONFIG_SYSTEM"] = null
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("CURSOR_API_KEY", None)
    return env


def _git_bin() -> str:
    found = shutil.which("git")
    if not found:
        raise SandboxViolation(_NO_GIT)
    return found


def _run_git(worktree: str, *args: str) -> subprocess.CompletedProcess[str]:
    if not is_within_authorized(worktree, worktree):
        raise SandboxViolation(_UNAUTH)
    argv = [_git_bin(), *args]
    assert_no_auto_push_or_merge(argv)
    try:
        return subprocess.run(
            argv,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxViolation(_GIT_FAILED.format(detail=str(exc))) from exc


def _require_git(worktree: str, *args: str) -> str:
    result = _run_git(worktree, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit={result.returncode}").strip()
        raise SandboxViolation(_GIT_FAILED.format(detail=detail))
    return result.stdout


def _ensure_repo_and_branch(worktree: str, branch: str) -> None:
    root = Path(worktree)
    if not root.is_dir():
        raise SandboxViolation(_UNAUTH)
    if not (root / ".git").exists():
        _require_git(worktree, "init")
        _require_git(worktree, "symbolic-ref", "HEAD", f"refs/heads/{BASE_BRANCH}")
        _require_git(
            worktree,
            "-c",
            "user.name=dev-pipeline",
            "-c",
            "user.email=dev-pipeline@local",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        )
    if _branch_exists(worktree, branch):
        _require_git(worktree, "checkout", branch)
        return
    _require_git(worktree, "checkout", "-b", branch)


def _branch_exists(worktree: str, branch: str) -> bool:
    result = _run_git(worktree, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.returncode == 0


def _commit_local(worktree: str, branch: str) -> None:
    _require_git(worktree, "add", "--", IMPL_PATH, TEST_PATH, ROLLBACK_PATH)
    status = _run_git(worktree, "status", "--porcelain")
    if not status.stdout.strip():
        return
    _require_git(
        worktree,
        "-c",
        "user.name=dev-pipeline",
        "-c",
        "user.email=dev-pipeline@local",
        "commit",
        "-m",
        f"deliver {branch}",
    )


def _collect_diff(worktree: str) -> str:
    result = _run_git(worktree, "diff", f"{BASE_BRANCH}...HEAD")
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout
    shown = _run_git(worktree, "show", "--format=", "--stat", "HEAD")
    if shown.returncode == 0 and shown.stdout.strip():
        return shown.stdout
    return (result.stdout or shown.stdout or "").strip()


def _run_tests_in_worktree(worktree: str) -> tuple[str, bool]:
    resolved = str(Path(worktree).resolve())
    if not is_within_authorized(resolved, worktree):
        return _TESTS_NOT_IN_TREE, False
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=resolved,
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cwd={resolved}\n单测未能运行: {exc}", False
    output = (proc.stdout or "") + (proc.stderr or "")
    evidence = f"cwd={resolved}\nexit={proc.returncode}\n{output}".strip()
    ran = is_within_authorized(resolved, worktree)
    return evidence, ran
