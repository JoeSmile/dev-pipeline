"""Coding-sandbox path guard and guarded writes. This module may perform I/O.

Workflow modules must not import this file. Writes are allowed only inside an
already authorized worktree, after path normalization. Default is unauthorized.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from dev_pipeline.errors import ProductionDeployForbidden, SandboxViolation
from dev_pipeline.models import SandboxRecord, SandboxStatus, SandboxWriteResult
from dev_pipeline.remote_write import classify_remote_action, decide_remote_write, infer_git_target

PathStyle = str  # "windows" | "posix"

_UNAUTH = "未授权工作区，拒绝写入"
_ESCAPE = "写入路径越出授权 worktree，已拒绝: {path}"
_OTHER_DRIVE = "写入路径与授权 worktree 不在同一盘符或卷，已拒绝: {path}"
_UNC = "写入路径指向 UNC 或其他卷，已拒绝: {path}"
_SYMLINK = "写入路径经符号链接越出授权 worktree，已拒绝: {path}"
_OTHER_REPO = "写入路径落在未授权仓库，已拒绝: {path}"
_EMPTY_PATH = "写入路径不能为空"
_FORBIDDEN_GIT = "写码沙箱默认禁止远程 push / 合并或修改全局 Git 规则，已拒绝: {command}"

_WIN_DRIVE = re.compile(r"^[A-Za-z]:")
_GIT_REMOTE_SUBCOMMANDS = frozenset({"push", "pull", "fetch"})
_GIT_MUTATING_MERGE = frozenset({"merge", "rebase"})


def looks_like_windows_path(path: str) -> bool:
    """Return True when *path* uses a Windows drive, UNC, or backslash form."""

    raw = path.strip()
    if not raw:
        return False
    compact = raw.replace("/", "\\")
    if compact.startswith("\\\\"):
        return True
    if len(compact) >= 2 and compact[1] == ":" and compact[0].isalpha():
        return True
    return "\\" in raw


def resolve_path_style(
    *paths: str,
    style: PathStyle | None = None,
) -> PathStyle:
    """Pick windows or posix rules. Explicit *style* always wins."""

    if style in {"windows", "posix"}:
        return style
    if any(looks_like_windows_path(path) for path in paths if path):
        return "windows"
    return "windows" if os.name == "nt" else "posix"


def _strip_win_long_prefix(path: str) -> str:
    compact = path.replace("/", "\\")
    upper = compact.upper()
    if upper.startswith("\\\\?\\UNC\\"):
        return "\\\\" + compact[8:]
    if upper.startswith("\\\\?\\"):
        return compact[4:]
    return compact


def _split_unc(path: str) -> tuple[str, list[str]] | None:
    if not path.startswith("\\\\"):
        return None
    rest = path[2:]
    while rest.startswith("\\"):
        rest = rest[1:]
    parts = [item for item in rest.split("\\") if item and item != "."]
    if len(parts) < 2:
        return "\\\\", parts
    share = f"\\\\{parts[0]}\\{parts[1]}"
    return share, parts[2:]


def _collapse_parts(parts: Sequence[str], *, allow_escape: bool) -> list[str] | None:
    out: list[str] = []
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if out:
                out.pop()
                continue
            if allow_escape:
                return None
            return None
        out.append(part)
    return out


def normalize_policy_path(path: str, *, style: PathStyle | None = None) -> str:
    """Normalize *path* for authorization comparison without touching the filesystem."""

    raw = path.strip()
    if not raw:
        return ""
    chosen = resolve_path_style(raw, style=style)
    if chosen == "posix":
        compact = raw.replace("\\", "/")
        parts = compact.split("/")
        absolute = compact.startswith("/")
        collapsed = _collapse_parts(parts[1:] if absolute else parts, allow_escape=False)
        if collapsed is None:
            return ""
        body = "/".join(collapsed)
        return ("/" + body) if absolute else body

    compact = _strip_win_long_prefix(raw)
    compact = compact.replace("/", "\\")
    unc = _split_unc(compact)
    if unc is not None:
        share, tail = unc
        collapsed = _collapse_parts(tail, allow_escape=False)
        if collapsed is None or share == "\\\\":
            return ""
        body = "\\".join(collapsed)
        return f"{share}\\{body}".rstrip("\\").casefold() if body else share.casefold()

    if _WIN_DRIVE.match(compact):
        drive = compact[:2].upper()
        rest = compact[2:]
        if rest.startswith("\\"):
            rest = rest[1:]
        collapsed = _collapse_parts(rest.split("\\"), allow_escape=False)
        if collapsed is None:
            return ""
        body = "\\".join(collapsed)
        return f"{drive}\\{body}".rstrip("\\") if body else f"{drive}\\"
    collapsed = _collapse_parts(compact.split("\\"), allow_escape=False)
    if collapsed is None:
        return ""
    return "\\".join(collapsed).casefold()


def _windows_contains(root: str, target: str) -> bool:
    root_n = normalize_policy_path(root, style="windows")
    target_n = normalize_policy_path(target, style="windows")
    if not root_n or not target_n:
        return False
    root_key = root_n.casefold()
    target_key = target_n.casefold()
    if target_key == root_key:
        return True
    sep = "\\"
    return target_key.startswith(root_key.rstrip(sep) + sep)


def _posix_contains(root: str, target: str) -> bool:
    root_n = normalize_policy_path(root, style="posix")
    target_n = normalize_policy_path(target, style="posix")
    if not root_n or not target_n:
        return False
    if target_n == root_n:
        return True
    prefix = root_n.rstrip("/") + "/"
    return target_n.startswith(prefix)


def _drive_or_volume(path: str, *, style: PathStyle) -> str:
    normalized = normalize_policy_path(path, style=style)
    if not normalized:
        return ""
    if style == "posix":
        return "/" if normalized.startswith("/") else ""
    if normalized.startswith("\\\\"):
        unc = _split_unc(_strip_win_long_prefix(path).replace("/", "\\"))
        return "" if unc is None else unc[0].casefold()
    if len(normalized) >= 2 and normalized[1] == ":":
        return normalized[:2].upper()
    return ""


def _is_unc(path: str, *, style: PathStyle) -> bool:
    if style != "windows":
        return False
    compact = _strip_win_long_prefix(path.strip()).replace("/", "\\")
    return compact.startswith("\\\\")


def classify_boundary_reason(
    target: str,
    authorized_worktree: str,
    *,
    style: PathStyle | None = None,
) -> str | None:
    """Return a Chinese rejection reason, or None when *target* is inside the tree."""

    if not authorized_worktree or not authorized_worktree.strip():
        return _UNAUTH
    if not target or not target.strip():
        return _EMPTY_PATH
    chosen = resolve_path_style(target, authorized_worktree, style=style)
    root_vol = _drive_or_volume(authorized_worktree, style=chosen)
    target_vol = _drive_or_volume(target, style=chosen)
    if chosen == "windows":
        target_unc = _is_unc(target, style=chosen)
        root_unc = _is_unc(authorized_worktree, style=chosen)
        if target_unc and not root_unc:
            return _UNC.format(path=target)
        if root_vol and target_vol and root_vol != target_vol:
            return _OTHER_DRIVE.format(path=target)
        if _windows_contains(authorized_worktree, target):
            return None
        if target_unc or (target_vol and root_vol and target_vol != root_vol):
            return _OTHER_DRIVE.format(path=target)
        return _ESCAPE.format(path=target)
    if _posix_contains(authorized_worktree, target):
        return None
    return _ESCAPE.format(path=target)


def is_within_authorized(
    target: str,
    authorized_worktree: str,
    *,
    style: PathStyle | None = None,
) -> bool:
    """Return True when the normalized *target* stays inside the authorized tree."""

    return classify_boundary_reason(target, authorized_worktree, style=style) is None


def _git_dir(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in [current, *current.parents]:
        git = candidate / ".git"
        try:
            if git.exists():
                return candidate
        except OSError:
            return None
    return None


def _realpath_existing(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _first_existing_ancestor(path: Path) -> Path:
    current = path
    while True:
        try:
            if current.exists():
                return _realpath_existing(current)
        except OSError:
            return current
        if current.parent == current:
            return current
        current = current.parent


def inspect_filesystem_escape(target: str, authorized_worktree: str) -> str | None:
    """Check symlink and other-repo escapes on the real filesystem of this host.

    Lexical policy must already have accepted *target*. This step is skipped
    when the authorized tree does not exist on this machine (synthetic paths).
    """

    root = Path(authorized_worktree)
    try:
        if not root.exists():
            return None
    except OSError:
        return None
    root_real = _realpath_existing(root)
    target_path = Path(target)
    existing = _first_existing_ancestor(target_path)
    existing_real = _realpath_existing(existing)
    try:
        existing_real.relative_to(root_real)
        inside_existing = True
    except ValueError:
        inside_existing = False
    if not inside_existing:
        return _SYMLINK.format(path=target)
    root_repo = _git_dir(root_real)
    target_repo = _git_dir(existing_real)
    if root_repo is not None and target_repo is not None and root_repo != target_repo:
        return _OTHER_REPO.format(path=target)
    return None


def join_authorized_target(authorized_worktree: str, requested: str) -> str:
    """Join a relative request onto the worktree; leave absolute paths intact."""

    raw = requested.strip()
    style = resolve_path_style(authorized_worktree, raw)
    if style == "windows":
        compact = _strip_win_long_prefix(raw).replace("/", "\\")
        if _WIN_DRIVE.match(compact) or compact.startswith("\\\\"):
            return raw
        return str(PureWindowsPath(_strip_win_long_prefix(authorized_worktree)) / compact)
    path = Path(raw)
    if path.is_absolute():
        return raw
    return str(Path(authorized_worktree) / raw)


def build_sandbox_record(
    *,
    authorized_worktree: str,
    requested_path: str,
    reason: str | None,
) -> SandboxRecord:
    """Build a queryable sandbox record for an allowed or rejected write."""

    if not authorized_worktree.strip():
        status: SandboxStatus = "rejected" if requested_path else "unauthorized"
        return SandboxRecord(
            authorized_worktree="",
            status=status,
            requested_path=requested_path,
            reason=reason or _UNAUTH,
        )
    if reason:
        return SandboxRecord(
            authorized_worktree=authorized_worktree,
            status="rejected",
            requested_path=requested_path,
            reason=reason,
        )
    if requested_path:
        return SandboxRecord(
            authorized_worktree=authorized_worktree,
            status="allowed",
            requested_path=requested_path,
            reason="",
        )
    return SandboxRecord(
        authorized_worktree=authorized_worktree,
        status="authorized",
        requested_path="",
        reason="",
    )


def remote_git_action_reason(argv: Sequence[str]) -> str | None:
    """Return a reason when *argv* would push, merge, or change global git rules."""

    tokens = [str(item) for item in argv if str(item).strip()]
    if not tokens:
        return None
    lower = [item.casefold() for item in tokens]
    command = " ".join(tokens)
    if "git" not in lower:
        return None
    git_index = lower.index("git")
    rest = lower[git_index + 1 :]
    words = [item for item in rest if not item.startswith("-")]
    if "push" in rest or any(word in _GIT_REMOTE_SUBCOMMANDS for word in words[:1]):
        return _FORBIDDEN_GIT.format(command=command)
    if words and words[0] in _GIT_MUTATING_MERGE:
        return _FORBIDDEN_GIT.format(command=command)
    if "merge" in rest or "rebase" in rest:
        return _FORBIDDEN_GIT.format(command=command)
    has_global_config = "--global" in rest and any(
        item in {"config", "--config"} or item == "config" for item in rest
    )
    if has_global_config:
        return _FORBIDDEN_GIT.format(command=command)
    if words and words[0] == "config" and "--global" in rest:
        return _FORBIDDEN_GIT.format(command=command)
    return None


def assert_no_auto_push_or_merge(argv: Sequence[str]) -> None:
    """Reject git push, merge, rebase, or ``git config --global`` commands.

    Production-named remotes or refs are rejected as production writes and
    recorded. Default unauthorized push / merge never performs a remote write.
    """

    reason = remote_git_action_reason(argv)
    if not reason:
        return
    action = classify_remote_action(list(argv))
    target = infer_git_target(tuple(str(item) for item in argv))
    if action is not None:
        decision = decide_remote_write(action, target)
        if decision.rejection is not None and decision.rejection.kind == "production":
            from dev_pipeline.remote_write_store import remember_rejection

            remember_rejection(decision.rejection)
            raise ProductionDeployForbidden(decision.reason, decision.rejection)
    raise SandboxViolation(reason)


def evaluate_write(
    authorized_worktree: str,
    requested_path: str,
    *,
    style: PathStyle | None = None,
    inspect_filesystem: bool = True,
) -> SandboxWriteResult:
    """Decide whether a write may proceed. Never writes the file itself."""

    if not authorized_worktree.strip():
        record = build_sandbox_record(
            authorized_worktree="",
            requested_path=requested_path,
            reason=_UNAUTH,
        )
        return SandboxWriteResult(allowed=False, record=record, written_path="")
    if not requested_path.strip():
        record = build_sandbox_record(
            authorized_worktree=authorized_worktree,
            requested_path=requested_path,
            reason=_EMPTY_PATH,
        )
        return SandboxWriteResult(allowed=False, record=record, written_path="")
    target = join_authorized_target(authorized_worktree, requested_path)
    reason = classify_boundary_reason(target, authorized_worktree, style=style)
    if reason is None and inspect_filesystem:
        reason = inspect_filesystem_escape(target, authorized_worktree)
    record = build_sandbox_record(
        authorized_worktree=authorized_worktree,
        requested_path=requested_path,
        reason=reason,
    )
    return SandboxWriteResult(
        allowed=reason is None,
        record=record,
        written_path=target if reason is None else "",
    )


def apply_guarded_write(
    authorized_worktree: str,
    requested_path: str,
    content: str,
    *,
    style: PathStyle | None = None,
) -> SandboxWriteResult:
    """Write *content* only when *requested_path* stays inside the authorized tree."""

    decision = evaluate_write(
        authorized_worktree,
        requested_path,
        style=style,
        inspect_filesystem=True,
    )
    if not decision.allowed or not decision.written_path:
        return decision
    if os.name != "nt" and looks_like_windows_path(authorized_worktree):
        record = build_sandbox_record(
            authorized_worktree=authorized_worktree,
            requested_path=requested_path,
            reason="当前主机不是 Windows，拒绝把盘符或 UNC 路径落到 POSIX 文件系统",
        )
        return SandboxWriteResult(allowed=False, record=record, written_path="")
    destination = Path(decision.written_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    # Recheck after the write in case a race replaced a parent with a symlink.
    post = inspect_filesystem_escape(str(destination), authorized_worktree)
    if post is not None:
        try:
            destination.unlink()
        except OSError:
            pass
        record = build_sandbox_record(
            authorized_worktree=authorized_worktree,
            requested_path=requested_path,
            reason=post,
        )
        return SandboxWriteResult(allowed=False, record=record, written_path="")
    return decision


REVIEW_WRITE_DENIED = "Review Worker 只读，拒绝写入工作区"


def apply_review_write(
    authorized_worktree: str,
    requested_path: str,
    content: str,
    *,
    style: PathStyle | None = None,
) -> SandboxWriteResult:
    """Reject a Review Worker write, even when the path is inside the tree.

    The path guard still runs so an out-of-bounds attempt keeps its sandbox
    reason. In-bounds paths are rejected as read-only and the file is not
    created. ``content`` is accepted only so callers share the write signature.
    """

    del content
    decision = evaluate_write(
        authorized_worktree,
        requested_path,
        style=style,
        inspect_filesystem=True,
    )
    if not decision.allowed:
        return decision
    record = build_sandbox_record(
        authorized_worktree=authorized_worktree,
        requested_path=requested_path,
        reason=REVIEW_WRITE_DENIED,
    )
    return SandboxWriteResult(allowed=False, record=record, written_path="")
