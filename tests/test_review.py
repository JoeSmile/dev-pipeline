"""Unit tests for dual-review classification, gates, argv, and read-only writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.budget import no_new_evidence_reason
from dev_pipeline.errors import SandboxViolation
from dev_pipeline.review import (
    CITATION_LINE_LIMIT,
    LINE_LIMIT_SUMMARY,
    MAX_FILE_LINES,
    ReviewFinding,
    ReviewWorkerReport,
    aggregate_dual_review,
    enforce_hard_line_limit,
    worker_report_from_legacy,
)
from dev_pipeline.review_backend import produce_review_report
from dev_pipeline.review_cli import (
    assert_review_argv_legal,
    assert_workspace_unchanged,
    build_review_argv,
    workspace_fingerprint,
)
from dev_pipeline.review_cli_adapter import confirm_reserved_review_unchanged, plan_review_cli
from dev_pipeline.sandbox import REVIEW_WRITE_DENIED, apply_review_write


def _diff_for(path: str, body: str) -> str:
    lines = body.splitlines()
    added = "\n".join("+" + line for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}\n"
    )


def test_aggregate_advisory_only_goes_to_verifying() -> None:
    reports = [
        worker_report_from_legacy("standards", info=["l0 ok"], suggestions=["命名可再收紧"]),
        worker_report_from_legacy("spec", info=["约束待证"]),
    ]
    record = aggregate_dual_review(reports)
    assert record.requires_human_gate is False
    assert record.destination == "Verifying"
    assert record.auto_arbitrated is False
    assert record.rounds == 1
    assert record.no_new_evidence is True
    assert record.stopped_reason == no_new_evidence_reason("review")
    severities = {item.severity for item in record.findings}
    assert severities <= {"建议", "信息"}
    assert all(item.rule_citation for item in record.findings)


def test_either_block_or_paradigm_drift_enters_human_gate() -> None:
    blocked = aggregate_dual_review(
        [
            worker_report_from_legacy("standards", blocking=["规范阻断"]),
            worker_report_from_legacy("spec", info=["ok"]),
        ]
    )
    assert blocked.requires_human_gate is True
    assert blocked.destination == "HumanGate"
    assert blocked.auto_arbitrated is False
    drifted = aggregate_dual_review(
        [
            worker_report_from_legacy("standards", info=["ok"], paradigm_drift=True),
            worker_report_from_legacy("spec", suggestions=["可选建议"]),
        ]
    )
    assert drifted.paradigm_drift is True
    assert drifted.destination == "HumanGate"


def test_conflicting_blocks_enter_human_gate_without_arbitration() -> None:
    reports = [
        worker_report_from_legacy("standards", blocking=["L0 被改"]),
        worker_report_from_legacy("spec", blocking=["不得新增 forbidden.py"]),
    ]
    record = aggregate_dual_review(reports)
    assert record.conflicting_blocks is True
    assert record.requires_human_gate is True
    assert record.destination == "HumanGate"
    assert record.auto_arbitrated is False
    assert len(record.conflict_reasons) == 2
    assert "L0 被改" in record.conflict_reasons[0] or "L0 被改" in record.conflict_reasons[1]
    assert any("forbidden.py" in item for item in record.conflict_reasons)


def test_line_limit_finding_cannot_be_waived_as_suggestion() -> None:
    waived = ReviewFinding(
        severity="建议",
        summary=LINE_LIMIT_SUMMARY,
        rule_citation=CITATION_LINE_LIMIT,
        path="huge.py",
    )
    enforced = enforce_hard_line_limit([waived])
    assert enforced[0].severity == "阻断"
    record = aggregate_dual_review(
        [
            ReviewWorkerReport(worker_id="standards", findings=[waived]),
            ReviewWorkerReport(worker_id="spec", findings=[waived]),
        ]
    )
    assert all(item.severity == "阻断" for item in record.findings)
    assert record.destination == "HumanGate"
    assert record.conflicting_blocks is False


def test_backend_blocks_file_over_2000_lines(tmp_path: Path) -> None:
    path = tmp_path / "huge.py"
    path.write_text("\n".join(f"line-{i}" for i in range(MAX_FILE_LINES + 1)), encoding="utf-8")
    diff = _diff_for("huge.py", path.read_text(encoding="utf-8"))
    standards = produce_review_report("standards", diff, worktree=str(tmp_path))
    spec = produce_review_report("spec", diff, worktree=str(tmp_path))
    assert any(item.severity == "阻断" and "2000" in item.summary for item in standards.findings)
    assert any(item.severity == "阻断" and "2000" in item.summary for item in spec.findings)
    assert all(
        CITATION_LINE_LIMIT in item.rule_citation or "2000" in item.summary
        for item in standards.findings
        if item.severity == "阻断"
    )
    record = aggregate_dual_review([standards, spec])
    assert record.destination == "HumanGate"
    waived = ReviewFinding(
        severity="建议",
        summary="huge.py 有点长，建议拆分",
        rule_citation=CITATION_LINE_LIMIT,
        path="huge.py",
    )
    assert enforce_hard_line_limit([waived])[0].severity == "阻断"


def test_backend_flags_paradigm_drift_on_charter_edit() -> None:
    diff = _diff_for("docs/CHARTER.md", "静默改宪\n")
    standards = produce_review_report("standards", diff)
    assert any(item.paradigm_drift and item.severity == "阻断" for item in standards.findings)


def test_backend_spec_blocks_story_constraint(tmp_path: Path) -> None:
    (tmp_path / "STORY_CONSTRAINTS.md").write_text(
        "阻断: 不得新增 forbidden.py\n",
        encoding="utf-8",
    )
    (tmp_path / "forbidden.py").write_text("x = 1\n", encoding="utf-8")
    diff = _diff_for("forbidden.py", "x = 1\n")
    spec = produce_review_report("spec", diff, worktree=str(tmp_path))
    standards = produce_review_report("standards", diff, worktree=str(tmp_path))
    assert any(item.severity == "阻断" and "forbidden.py" in item.summary for item in spec.findings)
    record = aggregate_dual_review([standards, spec])
    assert record.destination == "HumanGate"


def test_backend_conflict_between_axes(tmp_path: Path) -> None:
    (tmp_path / "STORY_CONSTRAINTS.md").write_text(
        "阻断: 不得新增 forbidden.py\n",
        encoding="utf-8",
    )
    (tmp_path / "forbidden.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CHARTER.md").write_text("changed\n", encoding="utf-8")
    diff = (
        _diff_for("forbidden.py", "x = 1\n")
        + _diff_for("docs/CHARTER.md", "changed\n")
    )
    standards = produce_review_report("standards", diff, worktree=str(tmp_path))
    spec = produce_review_report("spec", diff, worktree=str(tmp_path))
    record = aggregate_dual_review([standards, spec])
    assert record.conflicting_blocks is True
    assert record.auto_arbitrated is False
    assert record.destination == "HumanGate"


def test_review_write_is_rejected_and_worktree_unchanged(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    existing = worktree / "keep.txt"
    existing.write_text("safe\n", encoding="utf-8")
    before = workspace_fingerprint(str(worktree))
    result = apply_review_write(str(worktree), "keep.txt", "hacked")
    assert result.allowed is False
    assert result.record.reason == REVIEW_WRITE_DENIED
    assert result.written_path == ""
    assert existing.read_text(encoding="utf-8") == "safe\n"
    assert not (worktree / "new.txt").exists()
    outside = apply_review_write(str(worktree), "../escape.txt", "nope")
    assert outside.allowed is False
    assert (tmp_path / "escape.txt").exists() is False
    assert workspace_fingerprint(str(worktree)) == before


def test_reserved_review_argv_is_ask_mode_without_write_flags() -> None:
    authorized = r"C:\wt\T1\exec-1"
    argv = build_review_argv(authorized)
    assert argv == ["agent", "-p", "--mode=ask", "--workspace", authorized]
    assert "--force" not in argv
    assert "--resume" not in argv
    assert "--continue" not in argv
    assert "--worktree" not in argv
    assert "--api-key" not in argv
    assert_review_argv_legal(argv, authorized)
    with pytest.raises(SandboxViolation, match="--force"):
        assert_review_argv_legal([*argv, "--force"], authorized)
    with pytest.raises(SandboxViolation, match="--resume"):
        assert_review_argv_legal([*argv, "--resume"], authorized)
    with pytest.raises(SandboxViolation, match="--continue"):
        assert_review_argv_legal([*argv, "--continue"], authorized)
    with pytest.raises(SandboxViolation, match="--worktree"):
        assert_review_argv_legal([*argv, "--worktree", "extra"], authorized)
    with pytest.raises(SandboxViolation, match="--api-key"):
        assert_review_argv_legal([*argv, "--api-key", "SECRET"], authorized)


def test_reserved_review_cli_adapter_is_unwired_and_leaves_workspace(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "file.txt").write_text("unchanged\n", encoding="utf-8")
    plan = plan_review_cli(str(worktree))
    assert plan.wired is False
    assert plan.invoked is False
    assert plan.argv[:3] == ["agent", "-p", "--mode=ask"]
    assert "--force" not in plan.argv
    confirm_reserved_review_unchanged(plan)
    (worktree / "file.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SandboxViolation, match="工作区发生改动"):
        assert_workspace_unchanged(str(worktree), plan.fingerprint_before)


def test_normalize_review_decision_accepts_chinese_and_english() -> None:
    from dev_pipeline.review import normalize_review_decision, review_decision_label

    assert normalize_review_decision("放行") == "approve"
    assert normalize_review_decision("打回") == "rework"
    assert normalize_review_decision("否决") == "reject"
    assert normalize_review_decision("approve") == "approve"
    assert normalize_review_decision("maybe") is None
    assert review_decision_label("放行") == "放行"
    assert review_decision_label("rework") == "打回"
    assert review_decision_label("reject") == "否决"
