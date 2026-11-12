"""Deterministic verifying backend that runs in an authorized worktree.

This module performs filesystem I/O and subprocess checks. It does not
call Cursor CLI or read CURSOR_API_KEY. Workflow modules must not import
this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dev_pipeline.models import TicketWorkInput
from dev_pipeline.review_backend import CONSTRAINTS_FILENAME
from dev_pipeline.sandbox import apply_guarded_write
from dev_pipeline.verify import (
    CITATION_AUTHORIZED_WORKTREE,
    CITATION_LINE_LIMIT_VERIFY,
    CITATION_REPRODUCIBLE_EVIDENCE,
    MAX_FILE_LINES,
    REASON_UNAUTHORIZED,
    REASON_WRITE_REJECTED,
    RESULT_BROKEN,
    RESULT_DRY_RUN,
    RESULT_HELD,
    RESULT_HISTORY_ONLY,
    RESULT_PENDING,
    VERIFY_BACKEND_STUB,
    VERIFY_MODE_DRY_RUN,
    VERIFY_MODE_HISTORY,
    VerifyEvidenceLine,
    VerifyRecord,
    VerifyResult,
    VerifyWorkInput,
    classify_verify_outcome,
    evidence_relpath,
    normalize_verify_mode,
    render_evidence_text,
)
from dev_pipeline.workspace_auth import resolve_workspace_claim

_COMMAND_TIMEOUT = 30
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
)
_STATUS_FILENAME = "RED_LINE_STATUS.md"
_CONSTRAINT_BLOCKING_PREFIXES = ("阻断:", "阻断：")
_HELD_PREFIXES = ("守住:", "守住：")
_BROKEN_PREFIXES = ("未守住:", "未守住：")


def produce_verify_result(payload: VerifyWorkInput) -> VerifyResult:
    """Run reproducible checks in the authorized worktree and write evidence.

    Both pass and fail write a UTF-8 evidence file when the path stays
    inside the authorized tree. A dry-run or History-only probe is never
    classified as PASS.
    """

    worktree = payload.authorized_worktree.strip()
    if not worktree:
        claim = resolve_workspace_claim(
            TicketWorkInput(
                ticket_id=payload.ticket_id,
                execution_id=payload.execution_id,
                story_id=payload.story_id,
                config_root=payload.config_root,
            )
        )
        worktree = claim.authorized_worktree
    relpath = evidence_relpath(payload.evidence_path)
    target_path = str(Path(worktree) / relpath) if worktree else relpath
    mode = normalize_verify_mode(payload.mode)
    record = VerifyRecord(
        ticket_id=payload.ticket_id,
        execution_id=payload.execution_id,
        story_id=payload.story_id,
        evidence_path=target_path if worktree else "",
        backend=VERIFY_BACKEND_STUB,
        mode=mode,
    )
    if not worktree:
        record.reason = REASON_UNAUTHORIZED
        record.passed = False
        return VerifyResult(passed=False, evidence=record.reason, record=record)
    if mode == VERIFY_MODE_DRY_RUN:
        record.lines = [
            VerifyEvidenceLine(
                red_line=CITATION_REPRODUCIBLE_EVIDENCE,
                command_or_phenomenon="dry-run",
                result=RESULT_DRY_RUN,
                evidence_path=target_path,
            )
        ]
    elif mode == VERIFY_MODE_HISTORY:
        record.lines = [
            VerifyEvidenceLine(
                red_line=CITATION_REPRODUCIBLE_EVIDENCE,
                command_or_phenomenon="Temporal History 存在",
                result=RESULT_HISTORY_ONLY,
                evidence_path=target_path,
            )
        ]
    else:
        record.lines = _collect_worktree_lines(
            payload,
            worktree=worktree,
            evidence_path=target_path,
        )
    passed, reason = classify_verify_outcome(record.lines, mode=mode)
    record.passed = passed
    record.reason = reason
    wrote = apply_guarded_write(worktree, relpath, render_evidence_text(record))
    if not wrote.allowed:
        record.passed = False
        record.write_rejected = True
        record.write_reason = wrote.record.reason
        record.evidence_path = ""
        record.reason = f"{REASON_WRITE_REJECTED}: {wrote.record.reason}"
        for item in record.lines:
            item.evidence_path = ""
        return VerifyResult(passed=False, evidence=record.reason, record=record)
    record.evidence_path = wrote.written_path or target_path
    for item in record.lines:
        item.evidence_path = record.evidence_path
    # Rewrite once so 证据路径 in the file matches the final written path.
    apply_guarded_write(worktree, relpath, render_evidence_text(record))
    return VerifyResult(passed=record.passed, evidence=record.reason, record=record)


def _collect_worktree_lines(
    payload: VerifyWorkInput,
    *,
    worktree: str,
    evidence_path: str,
) -> list[VerifyEvidenceLine]:
    scan = _run_line_scan(worktree)
    lines = [
        _line_count_check(worktree, evidence_path, scan),
        _authorized_tree_check(worktree, evidence_path),
        _reproducible_command_check(worktree, evidence_path, scan),
    ]
    lines.extend(_scheme_hypothesis_lines(payload.scheme_red_lines, worktree, evidence_path))
    lines.extend(_story_constraint_lines(payload.story_constraints, worktree, evidence_path))
    return lines


def _line_count_check(
    worktree: str,
    evidence_path: str,
    scan: tuple[str, int, str, int, str],
) -> VerifyEvidenceLine:
    command, max_lines, worst, exit_code, output = scan
    held = exit_code == 0 and max_lines <= MAX_FILE_LINES
    detail = output.strip() or f"max_lines={max_lines} worst={worst} exit={exit_code}"
    phenomenon = f"{command} ；cwd={Path(worktree).resolve()} ；exit={exit_code} ；{detail}"
    return VerifyEvidenceLine(
        red_line=CITATION_LINE_LIMIT_VERIFY,
        command_or_phenomenon=phenomenon,
        result=RESULT_HELD if held else RESULT_BROKEN,
        evidence_path=evidence_path,
    )


def _authorized_tree_check(worktree: str, evidence_path: str) -> VerifyEvidenceLine:
    resolved = str(Path(worktree).resolve())
    command = f"{sys.executable} -c \"from pathlib import Path; p=Path('.').resolve(); print(p)\""
    proc = _run_command(
        [sys.executable, "-c", "from pathlib import Path; p=Path('.').resolve(); print(p)"],
        cwd=worktree,
    )
    observed = proc.stdout.strip() or proc.stderr.strip()
    held = proc.returncode == 0 and observed == resolved
    phenomenon = f"{command} ；cwd={resolved} ；exit={proc.returncode} ；observed={observed}"
    return VerifyEvidenceLine(
        red_line=CITATION_AUTHORIZED_WORKTREE,
        command_or_phenomenon=phenomenon,
        result=RESULT_HELD if held else RESULT_BROKEN,
        evidence_path=evidence_path,
    )


def _reproducible_command_check(
    worktree: str,
    evidence_path: str,
    scan: tuple[str, int, str, int, str],
) -> VerifyEvidenceLine:
    """Record that a real command ran in the tree; this is not a dry-run."""

    command, max_lines, _worst, exit_code, output = scan
    phenomenon = (
        f"{command} ；cwd={Path(worktree).resolve()} ；exit={exit_code} ；"
        f"{output.strip() or f'max_lines={max_lines}'}"
    )
    held = exit_code in {0, 1} and "dry-run" not in phenomenon.casefold()
    return VerifyEvidenceLine(
        red_line=CITATION_REPRODUCIBLE_EVIDENCE,
        command_or_phenomenon=phenomenon,
        result=RESULT_HELD if held else RESULT_BROKEN,
        evidence_path=evidence_path,
    )


def _scheme_hypothesis_lines(
    statements: list[str],
    worktree: str,
    evidence_path: str,
) -> list[VerifyEvidenceLine]:
    if not statements:
        return []
    status = _read_text(Path(worktree) / _STATUS_FILENAME)
    lines: list[VerifyEvidenceLine] = []
    for statement in statements:
        text = statement.strip()
        if not text:
            continue
        result = _status_for_statement(text, status)
        lines.append(
            VerifyEvidenceLine(
                red_line=f"方案红线级假设：{text}",
                command_or_phenomenon=(
                    f"读取授权 worktree 内 {_STATUS_FILENAME} 对照假设「{text}」"
                ),
                result=result,
                evidence_path=evidence_path,
            )
        )
    return lines


def _story_constraint_lines(
    story_constraints: str,
    worktree: str,
    evidence_path: str,
) -> list[VerifyEvidenceLine]:
    lines: list[VerifyEvidenceLine] = []
    file_text = _read_text(Path(worktree) / CONSTRAINTS_FILENAME)
    if file_text.strip():
        for raw in file_text.splitlines():
            stripped = raw.strip()
            body = _blocking_body(stripped)
            if body is None:
                continue
            broken = _constraint_file_present(body, worktree)
            lines.append(
                VerifyEvidenceLine(
                    red_line=f"本 Story 局部约束：{body}",
                    command_or_phenomenon=(
                        f"检查授权 worktree 内是否出现约束所指文件或标记；"
                        f"约束文件={CONSTRAINTS_FILENAME}"
                    ),
                    result=RESULT_BROKEN if broken else RESULT_HELD,
                    evidence_path=evidence_path,
                )
            )
    elif story_constraints.strip():
        lines.append(
            VerifyEvidenceLine(
                red_line=f"本 Story 局部约束：{story_constraints.strip()}",
                command_or_phenomenon="未提供 STORY_CONSTRAINTS.md，局部约束仍为待证",
                result=RESULT_PENDING,
                evidence_path=evidence_path,
            )
        )
    return lines


def _blocking_body(line: str) -> str | None:
    for prefix in _CONSTRAINT_BLOCKING_PREFIXES:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _constraint_file_present(body: str, worktree: str) -> bool:
    root = Path(worktree)
    for token in body.replace("\\", "/").split():
        cleaned = token.strip("「」\"'.,;；、")
        if not cleaned or "/" not in cleaned and "." not in cleaned:
            # Bare filenames with a suffix still count.
            if "." not in cleaned:
                continue
        candidate = root / cleaned
        try:
            if candidate.is_file() and _is_relative_to(candidate, root):
                return True
        except OSError:
            continue
    return False


def _status_for_statement(statement: str, status_text: str) -> str:
    for raw in status_text.splitlines():
        line = raw.strip()
        for prefix in _BROKEN_PREFIXES:
            if line.startswith(prefix) and statement in line:
                return RESULT_BROKEN
        for prefix in _HELD_PREFIXES:
            if line.startswith(prefix) and statement in line:
                return RESULT_HELD
    return RESULT_PENDING


def _run_line_scan(worktree: str) -> tuple[str, int, str, int, str]:
    script = (
        "from pathlib import Path\n"
        f"skip={sorted(_SKIP_DIR_NAMES)!r}\n"
        f"limit={MAX_FILE_LINES}\n"
        "root=Path('.').resolve()\n"
        "max_lines=0\n"
        "worst=''\n"
        "for path in root.rglob('*'):\n"
        "    if any(part in skip for part in path.parts):\n"
        "        continue\n"
        "    if path.name == 'verify.md' and 'evidence' in path.parts:\n"
        "        continue\n"
        "    if not path.is_file():\n"
        "        continue\n"
        "    try:\n"
        "        count=len(path.read_text(encoding='utf-8', errors='replace').splitlines())\n"
        "    except OSError:\n"
        "        continue\n"
        "    if count>max_lines:\n"
        "        max_lines=count\n"
        "        worst=str(path.relative_to(root))\n"
        "print(f'max_lines={max_lines}')\n"
        "print(f'worst={worst}')\n"
        "raise SystemExit(0 if max_lines<=limit else 1)\n"
    )
    command = f"{sys.executable} -c <line-scan>"
    proc = _run_command([sys.executable, "-c", script], cwd=worktree)
    max_lines, worst = _parse_scan_output(proc.stdout)
    output = (proc.stdout + proc.stderr).strip()
    return command, max_lines, worst, proc.returncode, output


def _parse_scan_output(stdout: str) -> tuple[int, str]:
    max_lines = 0
    worst = ""
    for raw in stdout.splitlines():
        if raw.startswith("max_lines="):
            try:
                max_lines = int(raw.split("=", 1)[1].strip() or "0")
            except ValueError:
                max_lines = 0
        elif raw.startswith("worst="):
            worst = raw.split("=", 1)[1].strip()
    return max_lines, worst


def _run_command(argv: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_COMMAND_TIMEOUT,
        env=_command_env(),
    )


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CURSOR_API_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _read_text(path: Path) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
