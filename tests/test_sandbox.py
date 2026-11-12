"""Unit tests for the coding-sandbox path guard and guarded writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.errors import SandboxViolation
from dev_pipeline.sandbox import (
    apply_guarded_write,
    assert_no_auto_push_or_merge,
    classify_boundary_reason,
    evaluate_write,
    is_within_authorized,
    normalize_policy_path,
    remote_git_action_reason,
)


def test_write_inside_authorized_worktree_succeeds(tmp_path: Path) -> None:
    worktree = tmp_path / "T1" / "exec-1"
    worktree.mkdir(parents=True)
    result = apply_guarded_write(str(worktree), "src/hello.txt", "ok")
    assert result.allowed is True
    assert result.record.status == "allowed"
    written = worktree / "src" / "hello.txt"
    assert written.read_text(encoding="utf-8") == "ok"
    assert Path(result.written_path) == written


def test_write_outside_is_rejected_and_not_created(tmp_path: Path) -> None:
    worktree = tmp_path / "T1" / "exec-1"
    worktree.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    result = apply_guarded_write(str(worktree), str(outside), "nope")
    assert result.allowed is False
    assert result.record.status == "rejected"
    assert "越出" in result.record.reason
    assert not outside.exists()


def test_path_escape_with_dotdot_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "T1" / "exec-1"
    worktree.mkdir(parents=True)
    outside = tmp_path / "T1" / "escaped.txt"
    result = apply_guarded_write(str(worktree), "../escaped.txt", "nope")
    assert result.allowed is False
    assert "越出" in result.record.reason
    assert not outside.exists()


def test_unauthorized_write_is_rejected_without_creating_files(tmp_path: Path) -> None:
    target = tmp_path / "external-repo" / "file.txt"
    target.parent.mkdir()
    result = evaluate_write("", str(target), inspect_filesystem=False)
    assert result.allowed is False
    assert result.record.status == "rejected"
    assert "未授权" in result.record.reason
    result2 = apply_guarded_write("", str(target), "nope")
    assert result2.allowed is False
    assert not target.exists()


def test_prefix_trap_is_not_treated_as_inside() -> None:
    assert is_within_authorized(r"C:\wt-other\file", r"C:\wt", style="windows") is False
    assert is_within_authorized("/tmp/wt-other/file", "/tmp/wt", style="posix") is False


@pytest.mark.parametrize(
    ("target", "authorized"),
    [
        (r"C:\wt\T1\exec-1\src\a.py", r"C:\wt\T1\exec-1"),
        (r"c:/wt/T1/exec-1/src/a.py", r"C:\WT\t1\exec-1"),
        (r"C:\wt\T1\exec-1\src\a.py", r"c:\wt\t1\exec-1"),
        (r"\\?\C:\wt\T1\exec-1\src\a.py", r"C:\wt\T1\exec-1"),
        (r"//?/C:/wt/T1/exec-1/src/a.py", r"C:\wt\T1\exec-1"),
    ],
)
def test_windows_drive_and_long_prefix_inside(target: str, authorized: str) -> None:
    assert is_within_authorized(target, authorized, style="windows") is True
    assert classify_boundary_reason(target, authorized, style="windows") is None


def test_windows_other_drive_is_rejected() -> None:
    reason = classify_boundary_reason(
        r"D:\wt\T1\exec-1\file.txt",
        r"C:\wt\T1\exec-1",
        style="windows",
    )
    assert reason is not None
    assert "盘符" in reason or "卷" in reason


def test_windows_unc_is_rejected_for_drive_worktree() -> None:
    reason = classify_boundary_reason(
        r"\\server\share\file.txt",
        r"C:\wt\T1\exec-1",
        style="windows",
    )
    assert reason is not None
    assert "UNC" in reason or "卷" in reason


def test_windows_long_path_inside_and_outside() -> None:
    root = r"C:\wt\T1\exec-1"
    inside = root + "\\" + ("a" * 280) + "\\file.txt"
    assert len(inside) > 260
    assert is_within_authorized(inside, root, style="windows") is True
    long_prefix = r"\\?\C:\wt\T1\exec-1" + "\\" + ("b" * 280)
    assert is_within_authorized(long_prefix, root, style="windows") is True
    outside = r"C:\wt\T1\\" + ("c" * 280)
    assert is_within_authorized(outside, root, style="windows") is False


def test_normalize_collapses_dotdot_and_mixed_separators() -> None:
    normalized = normalize_policy_path(r"C:\wt\T1\exec-1\..\secret", style="windows")
    assert normalized == r"C:\wt\T1\secret"
    assert is_within_authorized(
        r"C:\wt\T1\exec-1\..\secret",
        r"C:\wt\T1\exec-1",
        style="windows",
    ) is False


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "T1" / "exec-1"
    worktree.mkdir(parents=True)
    outside = tmp_path / "other-repo"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("keep", encoding="utf-8")
    link = worktree / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create directory symlinks")
    result = apply_guarded_write(str(worktree), "escape/secret.txt", "hacked")
    assert result.allowed is False
    assert "符号链接" in result.record.reason or "未授权仓库" in result.record.reason
    assert secret.read_text(encoding="utf-8") == "keep"


def test_write_to_a_different_repo_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "repo-a"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    other = tmp_path / "repo-b"
    other.mkdir()
    (other / ".git").mkdir()
    result = apply_guarded_write(str(worktree), str(other / "file.txt"), "nope")
    assert result.allowed is False
    assert not (other / "file.txt").exists()


def test_git_push_and_merge_are_forbidden() -> None:
    assert remote_git_action_reason(["git", "push", "origin", "main"]) is not None
    assert remote_git_action_reason(["git", "merge", "topic"]) is not None
    assert remote_git_action_reason(["git", "config", "--global", "user.name", "x"]) is not None
    assert remote_git_action_reason(["git", "status"]) is None
    with pytest.raises(SandboxViolation, match="禁止远程 push"):
        assert_no_auto_push_or_merge(["git", "push", "origin", "HEAD"])


def test_sandbox_modules_do_not_invoke_remote_git() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "dev_pipeline"
    files = [
        root / "sandbox.py",
        root / "coding_cli.py",
        root / "workspace_auth.py",
        root / "activities" / "sandbox.py",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "subprocess" not in text, f"{path.name} imports or calls subprocess"
        assert "Popen" not in text
        assert "os.system" not in text
        assert "['git', 'push']" not in text
        assert '["git", "push"]' not in text
