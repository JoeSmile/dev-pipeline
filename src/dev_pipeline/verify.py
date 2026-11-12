"""Verifying types, evidence-file contract, and pass/fail classification.

This module is deterministic and safe for workflow imports. It does not
touch the filesystem, clock, or network. The DP-32 evidence contract
(path, UTF-8, minimum fields) lives here so DP-24 does not invent a
second format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.review import CITATION_LINE_LIMIT, MAX_FILE_LINES

VerifyMode = Literal["", "dry-run", "history"]

FIELD_RED_LINE = "对应红线"
FIELD_COMMAND = "命令或现象"
FIELD_RESULT = "结果"
FIELD_EVIDENCE_PATH = "证据路径"
FIELD_TICKET_ID = "ticket_id"
FIELD_EXECUTION_ID = "execution_id"

RESULT_HELD = "守住"
RESULT_BROKEN = "未守住"
RESULT_PENDING = "待证"
RESULT_DRY_RUN = "dry-run"
RESULT_HISTORY_ONLY = "仅 History 存在"

VERIFY_MODE_NORMAL: VerifyMode = ""
VERIFY_MODE_DRY_RUN: VerifyMode = "dry-run"
VERIFY_MODE_HISTORY: VerifyMode = "history"

DEFAULT_EVIDENCE_RELPATH = "evidence/verify.md"
VERIFY_BACKEND_STUB = "桩"

CITATION_REPRODUCIBLE_EVIDENCE = (
    "CHARTER 硬红线第 5 条：任务对错以可复现证据为准，不把 dry-run 或历史记录当成 PASS"
)
CITATION_AUTHORIZED_WORKTREE = "CHARTER 硬红线第 3 条：写码 Worker 只能改授权 repo/worktree"
CITATION_LINE_LIMIT_VERIFY = CITATION_LINE_LIMIT

REASON_PROBE_NOT_PASS = "dry-run 或仅 History 存在不能标 PASS"
REASON_RED_LINE_BROKEN = "未守住红线，该 Ticket 回到 Implementing"
REASON_MISSING_MAPPING = "缺少可复现的红线 ↔ 证据"
REASON_PASSED = "过线证据齐"
REASON_UNAUTHORIZED = "未授权工作区，无法在授权 worktree 内执行可复现验证或写入证据"
REASON_WRITE_REJECTED = "验证证据写入被拒绝"

_PROBE_COMMAND_MARKERS = (
    "dry-run",
    "dry_run",
    "dry run",
    "temporal history",
    "history exists",
    "仅 history",
    "仅 history 存在",
)


@dataclass
class VerifyEvidenceLine:
    """One 「红线 ↔ 证据」 mapping with the DP-32 minimum fields."""

    red_line: str
    command_or_phenomenon: str
    result: str
    evidence_path: str


@dataclass
class VerifyRecord:
    """Queryable verification record on the existing child / parent snapshot."""

    ticket_id: str = ""
    execution_id: str = ""
    story_id: str = ""
    passed: bool = False
    evidence_path: str = ""
    lines: list[VerifyEvidenceLine] = field(default_factory=list)
    reason: str = ""
    backend: str = VERIFY_BACKEND_STUB
    mode: str = VERIFY_MODE_NORMAL
    write_rejected: bool = False
    write_reason: str = ""


@dataclass
class VerifyWorkInput:
    """Activity input for ticket verification. Contains no secrets."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str = ""
    authorized_worktree: str = ""
    scheme_red_lines: list[str] = field(default_factory=list)
    story_constraints: str = ""
    evidence_path: str = ""
    mode: str = VERIFY_MODE_NORMAL


@dataclass
class VerifyResult:
    """Verification activity result. Failure returns the child to Implementing."""

    passed: bool = False
    evidence: str = ""
    record: VerifyRecord | None = None


def normalize_verify_mode(value: str) -> VerifyMode:
    """Return a known verify mode, or the normal (empty) mode."""

    text = value.strip().casefold().replace("_", "-")
    if text in {"dry-run", "dryrun"}:
        return VERIFY_MODE_DRY_RUN
    if text in {"history", "history-only", "history only"}:
        return VERIFY_MODE_HISTORY
    return VERIFY_MODE_NORMAL


def is_probe_only_command(command_or_phenomenon: str) -> bool:
    """Return True when the recorded command is only a dry-run or History note."""

    text = command_or_phenomenon.strip().casefold()
    if not text:
        return False
    return any(marker in text for marker in _PROBE_COMMAND_MARKERS)


def lines_are_probe_only(lines: list[VerifyEvidenceLine]) -> bool:
    """Return True when every evidence line is a dry-run or History-only record."""

    if not lines:
        return False
    return all(is_probe_only_command(item.command_or_phenomenon) for item in lines)


def classify_verify_outcome(
    lines: list[VerifyEvidenceLine],
    *,
    mode: str = VERIFY_MODE_NORMAL,
) -> tuple[bool, str]:
    """Decide whether verification may PASS.

    A dry-run or a History-only observation is never PASS, even when a
    mapping line exists. Passing requires at least one held 红线 ↔ 证据
    line and no broken red line.
    """

    normalized = normalize_verify_mode(mode)
    if normalized in {VERIFY_MODE_DRY_RUN, VERIFY_MODE_HISTORY}:
        return False, REASON_PROBE_NOT_PASS
    if lines_are_probe_only(lines):
        return False, REASON_PROBE_NOT_PASS
    if any(item.result == RESULT_BROKEN for item in lines):
        return False, REASON_RED_LINE_BROKEN
    if any(item.result == RESULT_HELD for item in lines):
        return True, REASON_PASSED
    return False, REASON_MISSING_MAPPING


def evidence_relpath(requested: str = "") -> str:
    """Return the in-tree evidence path, defaulting to the DP-32 contract path."""

    text = requested.strip()
    return text or DEFAULT_EVIDENCE_RELPATH


def render_evidence_text(record: VerifyRecord) -> str:
    """Render a UTF-8 evidence document with the DP-32 minimum fields."""

    parts = [
        "# DevPipeline 验证证据",
        "# encoding: utf-8",
        "",
        f"{FIELD_TICKET_ID}: {record.ticket_id}",
        f"{FIELD_EXECUTION_ID}: {record.execution_id}",
        f"story_id: {record.story_id}",
        f"passed: {'true' if record.passed else 'false'}",
        f"backend: {record.backend}",
    ]
    if record.reason:
        parts.append(f"reason: {record.reason}")
    if record.mode:
        parts.append(f"mode: {record.mode}")
    for item in record.lines:
        parts.extend(
            [
                "",
                "[[line]]",
                f"{FIELD_RED_LINE}: {item.red_line}",
                f"{FIELD_COMMAND}: {item.command_or_phenomenon}",
                f"{FIELD_RESULT}: {item.result}",
                f"{FIELD_EVIDENCE_PATH}: {item.evidence_path}",
            ]
        )
    return "\n".join(parts) + "\n"


def parse_evidence_text(text: str) -> VerifyRecord:
    """Parse a DP-32 evidence document. Unknown keys are ignored."""

    record = VerifyRecord()
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[line]]":
            if current is not None:
                record.lines.append(_line_from_fields(current))
            current = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if current is not None:
            current[key] = value
            continue
        if key == FIELD_TICKET_ID:
            record.ticket_id = value
        elif key == FIELD_EXECUTION_ID:
            record.execution_id = value
        elif key == "story_id":
            record.story_id = value
        elif key == "passed":
            record.passed = value.casefold() in {"true", "1", "yes"}
        elif key == "backend":
            record.backend = value
        elif key == "reason":
            record.reason = value
        elif key == "mode":
            record.mode = value
    if current is not None:
        record.lines.append(_line_from_fields(current))
    return record


def _line_from_fields(fields: dict[str, str]) -> VerifyEvidenceLine:
    return VerifyEvidenceLine(
        red_line=fields.get(FIELD_RED_LINE, ""),
        command_or_phenomenon=fields.get(FIELD_COMMAND, ""),
        result=fields.get(FIELD_RESULT, ""),
        evidence_path=fields.get(FIELD_EVIDENCE_PATH, ""),
    )


__all__ = [
    "CITATION_AUTHORIZED_WORKTREE",
    "CITATION_LINE_LIMIT_VERIFY",
    "CITATION_REPRODUCIBLE_EVIDENCE",
    "DEFAULT_EVIDENCE_RELPATH",
    "FIELD_COMMAND",
    "FIELD_EVIDENCE_PATH",
    "FIELD_EXECUTION_ID",
    "FIELD_RED_LINE",
    "FIELD_RESULT",
    "FIELD_TICKET_ID",
    "MAX_FILE_LINES",
    "REASON_MISSING_MAPPING",
    "REASON_PASSED",
    "REASON_PROBE_NOT_PASS",
    "REASON_RED_LINE_BROKEN",
    "REASON_UNAUTHORIZED",
    "REASON_WRITE_REJECTED",
    "RESULT_BROKEN",
    "RESULT_DRY_RUN",
    "RESULT_HELD",
    "RESULT_HISTORY_ONLY",
    "RESULT_PENDING",
    "VERIFY_BACKEND_STUB",
    "VERIFY_MODE_DRY_RUN",
    "VERIFY_MODE_HISTORY",
    "VERIFY_MODE_NORMAL",
    "VerifyEvidenceLine",
    "VerifyMode",
    "VerifyRecord",
    "VerifyResult",
    "VerifyWorkInput",
    "classify_verify_outcome",
    "evidence_relpath",
    "is_probe_only_command",
    "lines_are_probe_only",
    "normalize_verify_mode",
    "parse_evidence_text",
    "render_evidence_text",
]
