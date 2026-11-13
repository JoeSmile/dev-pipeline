"""DP-31: default-off remote writes and production-target rejection records."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.constants import TEST_PUBLISH_TARGET
from dev_pipeline.errors import ProductionDeployForbidden, SandboxViolation
from dev_pipeline.models import PENDING_FIELD
from dev_pipeline.remote_write import (
    RemoteWriteAction,
    decide_remote_write,
    is_pr_authorized,
    looks_like_production_target,
)
from dev_pipeline.remote_write_store import (
    recorded_rejections,
    remember_rejection,
    reset_recorded_rejections,
    write_rejection_evidence,
)
from dev_pipeline.sandbox import assert_no_auto_push_or_merge, remote_git_action_reason


def test_pr_authorization_is_pending_and_unauthorized() -> None:
    assert is_pr_authorized(PENDING_FIELD) is False
    assert is_pr_authorized("") is False
    assert is_pr_authorized("authorized") is False
    assert is_pr_authorized("yes") is False


def test_default_push_merge_and_pr_are_denied_and_not_performed() -> None:
    cases: list[tuple[RemoteWriteAction, str]] = [
        ("push", "origin main"),
        ("merge", "main"),
        ("open_pr", "origin"),
    ]
    for action, target in cases:
        decision = decide_remote_write(action, target, authorization=PENDING_FIELD)
        assert decision.allowed is False
        assert decision.performed is False
        assert decision.rejection is not None
        assert decision.rejection.performed is False
        assert decision.rejection.authorization == PENDING_FIELD
        assert decision.rejection.kind == "unauthorized"
        assert "待证" in decision.rejection.reason or "未授权" in decision.rejection.reason


def test_test_env_publish_is_not_rejected_and_is_not_executed_here() -> None:
    decision = decide_remote_write("publish", TEST_PUBLISH_TARGET)
    assert decision.allowed is True
    assert decision.performed is False
    assert decision.rejection is None
    assert "DP-43" in decision.reason


def test_production_publish_is_rejected_with_record(tmp_path: Path) -> None:
    decision = decide_remote_write("publish", "production")
    assert decision.allowed is False
    assert decision.performed is False
    assert decision.rejection is not None
    assert decision.rejection.kind == "production"
    remembered = remember_rejection(decision.rejection, evidence_dir=str(tmp_path))
    assert remembered.performed is False
    assert "禁止生产发布" in remembered.reason
    journal = recorded_rejections()
    assert len(journal) == 1
    assert journal[0].target == "production"
    evidence = Path(remembered.evidence_path)
    assert evidence.is_file()
    text = evidence.read_text(encoding="utf-8")
    assert "production" in text
    assert "已执行: 否" in text


def test_non_test_publish_target_is_rejected() -> None:
    decision = decide_remote_write("publish", "staging")
    assert decision.allowed is False
    assert decision.rejection is not None
    assert decision.rejection.kind == "production"
    assert "禁止生产发布" in decision.reason


def test_production_push_and_merge_are_rejected_and_recorded(tmp_path: Path) -> None:
    for argv, action in (
        (["git", "push", "origin", "production"], "push"),
        (["git", "push", "prod"], "push"),
        (["git", "merge", "origin/live"], "merge"),
    ):
        reset_recorded_rejections()
        with pytest.raises(ProductionDeployForbidden, match="禁止生产发布") as exc:
            assert_no_auto_push_or_merge(argv)
        assert exc.value.record is not None
        journal = recorded_rejections()
        assert len(journal) == 1
        assert journal[0].action == action
        assert journal[0].kind == "production"
        assert journal[0].performed is False
        write_rejection_evidence(journal[0], str(tmp_path))
    files = list(tmp_path.glob("remote-write-rejection*.md"))
    assert files
    assert all("已执行: 否" in path.read_text(encoding="utf-8") for path in files)


def test_default_push_is_forbidden_without_production_record() -> None:
    assert remote_git_action_reason(["git", "push", "origin", "HEAD"]) is not None
    with pytest.raises(SandboxViolation, match="禁止远程 push"):
        assert_no_auto_push_or_merge(["git", "push", "origin", "HEAD"])
    assert recorded_rejections() == ()


def test_looks_like_production_target_matches_known_names() -> None:
    assert looks_like_production_target("production") is True
    assert looks_like_production_target("origin/prod") is True
    assert looks_like_production_target("live") is True
    assert looks_like_production_target("origin") is False
    assert looks_like_production_target("test") is False


def test_parent_snapshot_and_status_keep_production_rejection() -> None:
    from dev_pipeline.cli_status import format_status
    from dev_pipeline.remote_write import production_rejection
    from dev_pipeline.workflows.parent_state import ParentState

    state = ParentState()
    record = production_rejection("push", "origin/production")
    state.remote_write_rejections.append(record)
    snap = state.snapshot()
    assert snap.remote_write_rejections[0].target == "origin/production"
    assert snap.remote_write_rejections[0].performed is False
    text = format_status(snap)
    assert "origin/production" in text
    assert '"kind": "production"' in text
