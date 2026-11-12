"""Dual-review types, aggregation, and divergence rules. Workflows may import this.

This module performs no I/O and must not read the clock, draw random numbers,
or call external services. Review Workers produce findings; this module only
classifies and aggregates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.budget import no_new_evidence_reason

ReviewSeverity = Literal["阻断", "建议", "信息"]
ReviewDestination = Literal["HumanGate", "Verifying"]
ReviewOverrideDecision = Literal["approve", "rework", "reject"]

REVIEW_DECISION_ALIASES: dict[str, ReviewOverrideDecision] = {
    "approve": "approve",
    "rework": "rework",
    "reject": "reject",
    "放行": "approve",
    "打回": "rework",
    "否决": "reject",
}
REVIEW_DECISION_LABELS: dict[ReviewOverrideDecision, str] = {
    "approve": "放行",
    "rework": "打回",
    "reject": "否决",
}

REVIEW_WORKER_IDS: tuple[str, ...] = ("standards", "spec")
REVIEW_WORKER_LABELS = {
    "standards": "规范审查",
    "spec": "规格审查",
}

# Citations that already exist in docs/CHARTER.md (L0, frozen with DP-8).
CITATION_LINE_LIMIT = (
    "CHARTER 硬红线第 9 条 / 一期工作默认第 7 条（DP-44）："
    "单文件超过 2000 行视为 Review 阻断"
)
CITATION_REVIEW_READONLY = "CHARTER 硬红线第 3 条：Review Worker 默认只读 diff"
CITATION_NO_NEW_EVIDENCE = "CHARTER 硬红线第 6 条：无新证据即停"
CITATION_WORKFLOW_DETERMINISM = (
    "CHARTER 硬红线第 1 条 / 第 5 节：Workflow 只做确定性编排，LLM 调用须落在 Activity"
)
CITATION_NO_SILENT_L0 = "CHARTER 硬红线第 2 条：禁止静默升级本宪法"
CITATION_MODULE_BOUNDARY = "CHARTER 第 5 节：技术范式与模块边界"
CITATION_STORY_CONSTRAINTS_PENDING = "本 Story 局部约束：待证"

MAX_FILE_LINES = 2000
LINE_LIMIT_SUMMARY = "单文件超过 2000 行，必须拆文件；不得用建议项放行"

REVIEW_PATH_NAME = "review"


@dataclass
class ReviewFinding:
    """One review finding with a severity and a rule citation."""

    severity: ReviewSeverity
    summary: str
    rule_citation: str
    worker_id: str = ""
    paradigm_drift: bool = False
    path: str = ""

    def is_blocking(self) -> bool:
        """Return True when this finding is a 阻断 or marks paradigm drift."""
        return self.severity == "阻断" or self.paradigm_drift


@dataclass
class ReviewWorkInput:
    """Activity input for one Review Worker. Contains no secrets."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str = ""
    worker_id: str = ""
    write_path: str = ""
    write_content: str = ""


@dataclass
class ReviewWorkerReport:
    """Output of one Review Worker. Contains no secrets."""

    worker_id: str
    findings: list[ReviewFinding] = field(default_factory=list)
    has_new_evidence: bool = True
    write_rejected: bool = False
    write_reason: str = ""
    workspace_unchanged: bool = True
    detail: str = ""

    def blocking_keys(self) -> set[str]:
        """Return comparable keys for blocking findings (citation + summary)."""
        return {
            f"{item.rule_citation}|{item.summary}"
            for item in self.findings
            if item.is_blocking()
        }

    def has_block(self) -> bool:
        """Return True when this worker issued a 阻断 or paradigm drift."""
        return any(item.is_blocking() for item in self.findings)

    def has_paradigm_drift(self) -> bool:
        """Return True when this worker judged 范式漂移."""
        return any(item.paradigm_drift for item in self.findings)


@dataclass
class DualReviewRecord:
    """Aggregated dual-review result stored on the existing ticket snapshot."""

    workers: list[ReviewWorkerReport] = field(default_factory=list)
    findings: list[ReviewFinding] = field(default_factory=list)
    paradigm_drift: bool = False
    conflicting_blocks: bool = False
    conflict_reasons: list[str] = field(default_factory=list)
    requires_human_gate: bool = False
    destination: ReviewDestination = "Verifying"
    auto_arbitrated: bool = False
    rounds: int = 1
    no_new_evidence: bool = False
    stopped_reason: str = ""
    write_rejected: bool = False
    write_reason: str = ""
    workspace_unchanged: bool = True
    override: ReviewOverrideRecord | None = None


@dataclass
class ReviewOverrideRecord:
    """Human review_override decision stored on the existing review snapshot."""

    ticket_id: str = ""
    decision: ReviewOverrideDecision | str = ""
    action: str = ""
    decided_by: str = ""
    decided_at: str = ""
    reason: str = ""


def normalize_review_decision(raw: object) -> ReviewOverrideDecision | None:
    """Map 放行/打回/否决 or approve/rework/reject to the stored English decision."""

    if not isinstance(raw, str):
        return None
    return REVIEW_DECISION_ALIASES.get(raw.strip())


def review_decision_label(decision: object) -> str:
    """Return the Chinese action label for a stored review decision."""

    normalized = normalize_review_decision(decision)
    if normalized is None:
        return ""
    return REVIEW_DECISION_LABELS[normalized]


def is_line_limit_violation(item: ReviewFinding) -> bool:
    """Return True when a finding is about the 2000-line hard gate."""
    text = f"{item.summary}{item.rule_citation}{item.path}"
    return "2000" in text


def enforce_hard_line_limit(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Force any 2000-line finding back to 阻断; it must not be waived as 建议."""
    enforced: list[ReviewFinding] = []
    for item in findings:
        if is_line_limit_violation(item) and item.severity != "阻断":
            enforced.append(
                ReviewFinding(
                    severity="阻断",
                    summary=LINE_LIMIT_SUMMARY,
                    rule_citation=CITATION_LINE_LIMIT,
                    worker_id=item.worker_id,
                    paradigm_drift=False,
                    path=item.path,
                )
            )
        else:
            enforced.append(item)
    return enforced


def conflicting_block_reasons(reports: list[ReviewWorkerReport]) -> list[str]:
    """Return conflicting blocking reasons when two workers both block differently.

    The workflow must not auto-arbitrate; a human decides.
    """
    blockers = [report for report in reports if report.has_block()]
    if len(blockers) < 2:
        return []
    keys = [report.blocking_keys() for report in blockers]
    first = keys[0]
    if all(item == first for item in keys[1:]):
        return []
    reasons: list[str] = []
    for report in blockers:
        label = REVIEW_WORKER_LABELS.get(report.worker_id, report.worker_id)
        for item in report.findings:
            if item.is_blocking():
                reasons.append(f"{label}: {item.summary}（{item.rule_citation}）")
    return reasons


def aggregate_dual_review(reports: list[ReviewWorkerReport]) -> DualReviewRecord:
    """Aggregate two Review Worker reports. Never auto-arbitrates conflicts.

    After this single parallel round the review path stops, reusing the
    existing no-new-evidence wording so a second review loop is not started.
    """
    findings: list[ReviewFinding] = []
    for report in reports:
        findings.extend(enforce_hard_line_limit(list(report.findings)))
    paradigm = any(item.paradigm_drift for item in findings) or any(
        report.has_paradigm_drift() for report in reports
    )
    conflicts = conflicting_block_reasons(reports)
    has_block = any(item.is_blocking() for item in findings)
    requires = has_block or paradigm or bool(conflicts)
    destination: ReviewDestination = "HumanGate" if requires else "Verifying"
    write_rejected = any(report.write_rejected for report in reports)
    write_reason = next(
        (report.write_reason for report in reports if report.write_reason),
        "",
    )
    workspace_unchanged = all(report.workspace_unchanged for report in reports)
    return DualReviewRecord(
        workers=list(reports),
        findings=findings,
        paradigm_drift=paradigm,
        conflicting_blocks=bool(conflicts),
        conflict_reasons=list(conflicts),
        requires_human_gate=requires,
        destination=destination,
        auto_arbitrated=False,
        rounds=1,
        no_new_evidence=True,
        stopped_reason=no_new_evidence_reason(REVIEW_PATH_NAME),
        write_rejected=write_rejected,
        write_reason=write_reason,
        workspace_unchanged=workspace_unchanged,
    )


def worker_report_from_legacy(
    worker_id: str,
    blocking: list[str] | None = None,
    suggestions: list[str] | None = None,
    info: list[str] | None = None,
    *,
    paradigm_drift: bool = False,
) -> ReviewWorkerReport:
    """Build a worker report from the legacy ReviewFindings lists used in tests."""
    findings: list[ReviewFinding] = []
    citation = "测试夹具审查结论（非 L0 条文）"
    for summary in blocking or []:
        findings.append(
            ReviewFinding(
                severity="阻断",
                summary=summary,
                rule_citation=citation,
                worker_id=worker_id,
            )
        )
    for summary in suggestions or []:
        findings.append(
            ReviewFinding(
                severity="建议",
                summary=summary,
                rule_citation=citation,
                worker_id=worker_id,
            )
        )
    for summary in info or []:
        findings.append(
            ReviewFinding(
                severity="信息",
                summary=summary,
                rule_citation=citation,
                worker_id=worker_id,
            )
        )
    if paradigm_drift:
        findings.append(
            ReviewFinding(
                severity="阻断",
                summary="判定范式漂移",
                rule_citation=CITATION_MODULE_BOUNDARY,
                worker_id=worker_id,
                paradigm_drift=True,
            )
        )
    return ReviewWorkerReport(worker_id=worker_id, findings=findings)
