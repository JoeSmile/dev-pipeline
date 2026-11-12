"""Deterministic review backend: classify a ticket diff against L0 and constraints.

This module does not call Cursor CLI or an LLM. Workflow modules must not
import this file.
"""

from __future__ import annotations

import re
from pathlib import Path

from dev_pipeline.review import (
    CITATION_LINE_LIMIT,
    CITATION_NO_SILENT_L0,
    CITATION_REVIEW_READONLY,
    CITATION_STORY_CONSTRAINTS_PENDING,
    CITATION_WORKFLOW_DETERMINISM,
    LINE_LIMIT_SUMMARY,
    MAX_FILE_LINES,
    ReviewFinding,
    ReviewWorkerReport,
    enforce_hard_line_limit,
)

CONSTRAINTS_FILENAME = "STORY_CONSTRAINTS.md"
WORKFLOW_PATH_MARK = "src/dev_pipeline/workflows/"
CHARTER_NAME = "CHARTER.md"
_LLM_TOKENS = (
    "openai",
    "anthropic",
    "CURSOR_API_KEY",
    "ChatCompletion",
    "AsyncOpenAI",
    "Anthropic",
    "cursor-agent",
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_DIFF_PLUS_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")
_CONSTRAINT_LINE_RE = re.compile(
    r"^(阻断|建议|信息|范式漂移)\s*[:：]\s*(.+)$"
)

_INFO_REVIEWED = "已只读审查 diff 与 L0，未发现阻断项"
_WRITE_BLOCK_SUMMARY = "Review Worker 试图写入工作区，写入已被拒绝"


def produce_review_report(
    worker_id: str,
    diff: str,
    *,
    worktree: str = "",
    write_rejected: bool = False,
    write_reason: str = "",
    workspace_unchanged: bool = True,
) -> ReviewWorkerReport:
    """Classify findings for one Review Worker from the ticket diff and tree.

    Both workers read the diff, L0 rules, and story-local constraints. The
    standards worker specializes in CHARTER / paradigm rules; the spec worker
    specializes in story-local constraints. Both enforce the 2000-line hard
    gate and must not waive it as a 建议.
    """
    findings: list[ReviewFinding] = []
    findings.extend(_line_limit_findings(diff, worktree, worker_id))
    if write_rejected:
        findings.append(
            ReviewFinding(
                severity="阻断",
                summary=_WRITE_BLOCK_SUMMARY,
                rule_citation=CITATION_REVIEW_READONLY,
                worker_id=worker_id,
            )
        )
    if worker_id == "standards":
        findings.extend(_paradigm_findings(diff, worktree, worker_id))
        findings.extend(_constraint_info(worktree, worker_id, advisory=True))
    else:
        findings.extend(_constraint_findings(diff, worktree, worker_id))
        # Spec still reads L0: only emit 信息 unless a hard L0 gate already fired.
        if not any(item.paradigm_drift for item in findings):
            findings.extend(_paradigm_findings(diff, worktree, worker_id, info_only=True))
    findings = enforce_hard_line_limit(findings)
    if not findings:
        findings.append(
            ReviewFinding(
                severity="信息",
                summary=_INFO_REVIEWED,
                rule_citation=CITATION_REVIEW_READONLY,
                worker_id=worker_id,
            )
        )
    return ReviewWorkerReport(
        worker_id=worker_id,
        findings=findings,
        has_new_evidence=True,
        write_rejected=write_rejected,
        write_reason=write_reason,
        workspace_unchanged=workspace_unchanged,
        detail="deterministic-review",
    )


def changed_paths(diff: str) -> list[str]:
    """Return unique file paths referenced by a unified diff."""
    paths: list[str] = []
    seen: set[str] = set()
    for raw in diff.splitlines():
        line = raw.rstrip("\n")
        match = _DIFF_GIT_RE.match(line)
        if match:
            candidate = match.group(2).strip()
            if candidate and candidate not in seen and candidate != "/dev/null":
                seen.add(candidate)
                paths.append(candidate)
            continue
        match = _DIFF_PLUS_RE.match(line)
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith("b/"):
                candidate = candidate[2:]
            if candidate and candidate not in seen and candidate != "/dev/null":
                seen.add(candidate)
                paths.append(candidate)
    return paths


def _line_limit_findings(diff: str, worktree: str, worker_id: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for path in changed_paths(diff):
        count = _resulting_line_count(diff, worktree, path)
        if count is not None and count > MAX_FILE_LINES:
            findings.append(
                ReviewFinding(
                    severity="阻断",
                    summary=f"{LINE_LIMIT_SUMMARY}（{path} 共 {count} 行）",
                    rule_citation=CITATION_LINE_LIMIT,
                    worker_id=worker_id,
                    path=path,
                )
            )
    return findings


def _resulting_line_count(diff: str, worktree: str, rel: str) -> int | None:
    if worktree.strip():
        file_path = Path(worktree) / rel
        try:
            if file_path.is_file():
                text = file_path.read_text(encoding="utf-8", errors="replace")
                return len(text.splitlines())
        except OSError:
            pass
    added = _added_lines_in_diff(diff, rel)
    return added if added > 0 else None


def _added_lines_in_diff(diff: str, rel: str) -> int:
    in_file = False
    added = 0
    for line in diff.splitlines():
        git_match = _DIFF_GIT_RE.match(line)
        if git_match:
            in_file = git_match.group(2).strip() == rel
            continue
        plus_match = _DIFF_PLUS_RE.match(line)
        if plus_match:
            candidate = plus_match.group(1).strip()
            if candidate.startswith("b/"):
                candidate = candidate[2:]
            in_file = candidate == rel
            continue
        if line.startswith("diff --git ") or line.startswith("--- "):
            if line.startswith("diff --git "):
                in_file = False
            continue
        if in_file and line.startswith("+") and not line.startswith("+++"):
            added += 1
    return added


def _file_text(worktree: str, rel: str) -> str:
    if not worktree.strip():
        return ""
    path = Path(worktree) / rel
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


def _iter_diff_file_bodies(diff: str) -> list[tuple[str, str]]:
    bodies: list[tuple[str, str]] = []
    current_path = ""
    chunks: list[str] = []

    def flush() -> None:
        if current_path:
            bodies.append((current_path, "\n".join(chunks)))

    for line in diff.splitlines():
        git_match = _DIFF_GIT_RE.match(line)
        plus_match = _DIFF_PLUS_RE.match(line)
        if git_match:
            flush()
            current_path = git_match.group(2).strip()
            chunks = []
            continue
        if plus_match:
            candidate = plus_match.group(1).strip()
            if candidate.startswith("b/"):
                candidate = candidate[2:]
            if candidate and candidate != "/dev/null":
                if current_path and current_path != candidate:
                    flush()
                    chunks = []
                current_path = candidate
            continue
        if current_path and line.startswith("+") and not line.startswith("+++"):
            chunks.append(line[1:])
    flush()
    return bodies


def _paradigm_findings(
    diff: str,
    worktree: str,
    worker_id: str,
    *,
    info_only: bool = False,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for path in changed_paths(diff):
        normalized = path.replace("\\", "/")
        body = _file_text(worktree, path)
        if not body:
            for rel, text in _iter_diff_file_bodies(diff):
                if rel == path:
                    body = text
                    break
        if normalized.endswith(CHARTER_NAME) or normalized == CHARTER_NAME:
            if info_only:
                continue
            findings.append(
                ReviewFinding(
                    severity="阻断",
                    summary=f"审查发现对 {path} 的修改，构成对 L0 的静默升级风险",
                    rule_citation=CITATION_NO_SILENT_L0,
                    worker_id=worker_id,
                    paradigm_drift=True,
                    path=path,
                )
            )
            continue
        if WORKFLOW_PATH_MARK in normalized:
            lowered = body
            if any(token in lowered for token in _LLM_TOKENS):
                if info_only:
                    continue
                findings.append(
                    ReviewFinding(
                        severity="阻断",
                        summary=(
                            f"{path} 把 LLM / 密钥或非确定性调用引入 "
                            "Workflow 模块，构成范式漂移"
                        ),
                        rule_citation=CITATION_WORKFLOW_DETERMINISM,
                        worker_id=worker_id,
                        paradigm_drift=True,
                        path=path,
                    )
                )
    return findings


def _constraints_text(worktree: str) -> str:
    if not worktree.strip():
        return ""
    path = Path(worktree) / CONSTRAINTS_FILENAME
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


def _constraint_info(worktree: str, worker_id: str, *, advisory: bool) -> list[ReviewFinding]:
    text = _constraints_text(worktree)
    if text.strip():
        if advisory:
            return [
                ReviewFinding(
                    severity="信息",
                    summary="已阅读本 Story 局部约束",
                    rule_citation=CITATION_STORY_CONSTRAINTS_PENDING
                    if "待证" in text
                    else "本 Story 局部约束",
                    worker_id=worker_id,
                )
            ]
        return []
    return [
        ReviewFinding(
            severity="信息",
            summary="未提供本 Story 局部约束文件",
            rule_citation=CITATION_STORY_CONSTRAINTS_PENDING,
            worker_id=worker_id,
        )
    ]


def _constraint_findings(diff: str, worktree: str, worker_id: str) -> list[ReviewFinding]:
    text = _constraints_text(worktree)
    if not text.strip():
        return _constraint_info(worktree, worker_id, advisory=False)
    findings: list[ReviewFinding] = []
    changed = set(changed_paths(diff))
    joined_diff = diff
    for raw in text.splitlines():
        line = raw.strip()
        match = _CONSTRAINT_LINE_RE.match(line)
        if not match:
            continue
        kind, body = match.group(1), match.group(2).strip()
        citation = f"本 Story 局部约束：{body}"
        triggered = _constraint_triggered(body, changed, joined_diff, worktree)
        if not triggered:
            continue
        if kind == "范式漂移":
            findings.append(
                ReviewFinding(
                    severity="阻断",
                    summary=body,
                    rule_citation=citation,
                    worker_id=worker_id,
                    paradigm_drift=True,
                )
            )
        elif kind == "阻断":
            findings.append(
                ReviewFinding(
                    severity="阻断",
                    summary=body,
                    rule_citation=citation,
                    worker_id=worker_id,
                )
            )
        elif kind == "建议":
            findings.append(
                ReviewFinding(
                    severity="建议",
                    summary=body,
                    rule_citation=citation,
                    worker_id=worker_id,
                )
            )
        else:
            findings.append(
                ReviewFinding(
                    severity="信息",
                    summary=body,
                    rule_citation=citation,
                    worker_id=worker_id,
                )
            )
    if not findings:
        findings.extend(_constraint_info(worktree, worker_id, advisory=True))
    return findings


def _constraint_triggered(body: str, changed: set[str], diff: str, worktree: str) -> bool:
    """A constraint fires when it names a changed path or a token present in the diff."""
    for path in changed:
        name = Path(path).name
        if path in body or name in body:
            return True
    # "不得新增 X" / "禁止 X"
    for token in re.findall(r"[\w./-]+\.[A-Za-z0-9]+", body):
        if token in changed or Path(token).name in {Path(item).name for item in changed}:
            return True
        if token in diff:
            return True
    return False
