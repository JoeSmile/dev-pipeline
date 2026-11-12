"""Normalize a raw Intake request into a Story draft; missing items stay 待证."""

from __future__ import annotations

import pytest

from dev_pipeline.intake import intake_from_cli
from dev_pipeline.models import PENDING_FIELD, IntakeRecord, StoryDraft
from dev_pipeline.story_draft import (
    CURSOR_CLI_NOT_WIRED,
    CursorCliStoryDraftBackend,
    DeterministicStoryDraftBackend,
    draft_story_from_intake,
    lock_scope_preview,
)


def test_cli_like_intake_marks_every_gap_pending() -> None:
    draft = draft_story_from_intake(intake_from_cli("DP-15"))
    assert draft.to_solve == PENDING_FIELD
    assert draft.known_constraints == PENDING_FIELD
    assert draft.out_of_scope == PENDING_FIELD
    assert draft.acceptance == PENDING_FIELD
    assert draft.missing_items == [
        "要解决什么: 待证",
        "已知约束: 待证",
        "不做什么: 待证",
        "验收: 待证",
    ]
    assert draft.complete is False


def test_unlabeled_request_fills_only_to_solve() -> None:
    intake = IntakeRecord(jira_key="DP-15", request="把登录改成单点登录", source="cli")
    draft = draft_story_from_intake(intake)
    assert draft.to_solve == "把登录改成单点登录"
    assert draft.known_constraints == PENDING_FIELD
    assert draft.out_of_scope == PENDING_FIELD
    assert draft.acceptance == PENDING_FIELD
    assert draft.missing_items == [
        "已知约束: 待证",
        "不做什么: 待证",
        "验收: 待证",
    ]
    assert draft.complete is False


def test_labeled_fields_are_copied_verbatim_and_acceptance_is_not_invented() -> None:
    request = "要解决什么：把登录改成单点登录\n已知约束：必须兼容现有账号\n不做什么：不改计费\n"
    draft = draft_story_from_intake(IntakeRecord(jira_key="DP-15", request=request))
    assert draft.to_solve == "把登录改成单点登录"
    assert draft.known_constraints == "必须兼容现有账号"
    assert draft.out_of_scope == "不改计费"
    assert draft.acceptance == PENDING_FIELD
    assert draft.missing_items == ["验收: 待证"]
    assert draft.complete is False


def test_does_not_invent_acceptance_from_unlabeled_prose() -> None:
    request = "用户希望登录更快，可能需要验收一下性能。"
    draft = draft_story_from_intake(IntakeRecord(jira_key="DP-15", request=request))
    assert draft.to_solve == request
    assert draft.acceptance == PENDING_FIELD
    assert "验收: 待证" in draft.missing_items
    assert draft.complete is False


def test_multiline_labeled_value_stops_at_the_next_label() -> None:
    request = "要解决什么：把登录改成单点登录\n还要保留旧入口\n已知约束：必须兼容现有账号\n"
    draft = draft_story_from_intake(IntakeRecord(jira_key="DP-15", request=request))
    assert draft.to_solve == "把登录改成单点登录\n还要保留旧入口"
    assert draft.known_constraints == "必须兼容现有账号"


def test_blank_labeled_value_is_pending() -> None:
    request = "要解决什么：   \n已知约束：\n"
    draft = draft_story_from_intake(
        IntakeRecord(jira_key="DP-15", request=request),
        summary="可回退的摘要",
    )
    assert draft.to_solve == "可回退的摘要"
    assert draft.known_constraints == PENDING_FIELD
    assert "已知约束: 待证" in draft.missing_items


def test_summary_fills_to_solve_only_when_request_is_missing() -> None:
    intake = IntakeRecord(jira_key="DP-15", request=PENDING_FIELD)
    draft = draft_story_from_intake(intake, summary="从 summary 来的诉求")
    assert draft.to_solve == "从 summary 来的诉求"
    assert "要解决什么: 待证" not in draft.missing_items


def test_request_wins_over_summary() -> None:
    intake = IntakeRecord(jira_key="DP-15", request="原始诉求")
    draft = draft_story_from_intake(intake, summary="不要用这段")
    assert draft.to_solve == "原始诉求"


def test_complete_only_when_every_required_field_is_present() -> None:
    request = "要解决什么：A\n已知约束：B\n不做什么：C\n验收：D\n"
    draft = draft_story_from_intake(IntakeRecord(jira_key="DP-15", request=request))
    assert draft.to_solve == "A"
    assert draft.known_constraints == "B"
    assert draft.out_of_scope == "C"
    assert draft.acceptance == "D"
    assert draft.missing_items == []
    assert draft.complete is True


def test_none_intake_uses_summary_and_marks_other_gaps() -> None:
    draft = draft_story_from_intake(None, summary="手工启动的摘要")
    assert draft.to_solve == "手工启动的摘要"
    assert draft.complete is False
    assert "已知约束: 待证" in draft.missing_items


def test_lock_scope_preview_maps_draft_fields_without_locking() -> None:
    draft = StoryDraft(
        to_solve="登录单点",
        known_constraints="兼容旧账号",
        out_of_scope="不改计费",
        acceptance=PENDING_FIELD,
        missing_items=["验收: 待证"],
        complete=False,
    )
    preview = lock_scope_preview(draft)
    assert preview == {"in_scope": "登录单点", "out_of_scope": "不改计费"}


def test_deterministic_backend_matches_pure_function() -> None:
    intake = IntakeRecord(jira_key="DP-15", request="把登录改成单点登录")
    via_backend = DeterministicStoryDraftBackend().draft(intake, summary="")
    assert via_backend == draft_story_from_intake(intake)


def test_cursor_cli_backend_is_not_wired_and_does_not_need_a_key() -> None:
    backend = CursorCliStoryDraftBackend(config_root="/abs/config")
    with pytest.raises(RuntimeError, match="尚未接入") as exc:
        backend.draft(intake_from_cli("DP-15"), summary="")
    assert str(exc.value) == CURSOR_CLI_NOT_WIRED
    assert "CURSOR_API_KEY" not in str(exc.value)
    assert "key" not in str(exc.value).lower()
