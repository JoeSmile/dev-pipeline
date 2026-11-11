"""Unit tests for the idempotent Jira stage-mirror path (fake issue store)."""

from __future__ import annotations

from dev_pipeline.jira_mirror import (
    LAG_MARKER_PREFIX,
    STAGE_MIRROR_PREFIX,
    InMemoryJira,
    JiraLagMarkerInput,
    JiraStageMirrorInput,
    mark_mirror_lag,
    mirror_stage,
    stage_comment_body,
)


def _stage_input(
    stage: str = "ScopeLocked",
    *,
    execution_id: str = "exec-1",
    issue_key: str = "DP-9999",
    config_root: str = "/tmp/config",
) -> JiraStageMirrorInput:
    return JiraStageMirrorInput(
        issue_key=issue_key,
        stage=stage,
        execution_id=execution_id,
        config_root=config_root,
    )


def test_stage_comment_uses_prefix_and_does_not_look_like_orchestration() -> None:
    body = stage_comment_body("ScopeLocked", "exec-1")
    assert body.startswith(STAGE_MIRROR_PREFIX)
    assert "ScopeLocked" in body
    assert "exec-1" in body
    assert "镜像" in body
    assert "编排器" in body
    assert "Temporal History" in body


def test_mirror_stage_creates_prefixed_comment_and_shows_current_stage() -> None:
    jira = InMemoryJira()
    result = mirror_stage(jira, _stage_input("ScopeLocked"))
    comments = jira.comments("DP-9999")
    assert result.stage == "ScopeLocked"
    assert result.comment_updated is False
    assert result.field_updated is False
    assert len(comments) == 1
    assert comments[0].body.startswith(STAGE_MIRROR_PREFIX)
    assert "ScopeLocked" in comments[0].body
    assert jira.current_stage_text("DP-9999") == "ScopeLocked"


def test_mirror_same_execution_updates_existing_comment_instead_of_duplicating() -> None:
    jira = InMemoryJira()
    first = mirror_stage(jira, _stage_input("Intake"))
    second = mirror_stage(jira, _stage_input("ScopeLocked"))
    comments = jira.comments("DP-9999")
    assert first.comment_updated is False
    assert second.comment_updated is True
    assert len(comments) == 1
    assert "ScopeLocked" in comments[0].body
    assert "Intake" not in comments[0].body
    assert jira.current_stage_text("DP-9999") == "ScopeLocked"


def test_mirror_different_execution_creates_a_separate_comment() -> None:
    jira = InMemoryJira()
    mirror_stage(jira, _stage_input("Intake", execution_id="exec-old"))
    mirror_stage(jira, _stage_input("Intake", execution_id="exec-new"))
    comments = jira.comments("DP-9999")
    assert len(comments) == 2
    assert any("exec-old" in item.body for item in comments)
    assert any("exec-new" in item.body for item in comments)


def test_mirror_writes_optional_custom_field_when_configured() -> None:
    jira = InMemoryJira()
    result = mirror_stage(
        jira,
        _stage_input("SchemeDecided"),
        stage_field="customfield_12345",
    )
    assert result.field_updated is True
    assert jira.fields("DP-9999")["customfield_12345"] == "SchemeDecided"
    assert jira.current_stage_text("DP-9999") == "SchemeDecided"


def test_retry_of_same_stage_is_idempotent() -> None:
    jira = InMemoryJira()
    mirror_stage(jira, _stage_input("ScopeLocked"))
    mirror_stage(jira, _stage_input("ScopeLocked"))
    assert len(jira.comments("DP-9999")) == 1
    assert jira.current_stage_text("DP-9999") == "ScopeLocked"


def test_stale_jira_value_is_not_used_as_an_input_to_mirror() -> None:
    """The mirror writes the workflow stage; it never reads Jira to decide the stage."""
    jira = InMemoryJira()
    mirror_stage(jira, _stage_input("ScopeLocked"))
    jira.set_field("DP-9999", "customfield_12345", "Done")
    result = mirror_stage(
        jira,
        _stage_input("SchemeDecided"),
        stage_field="customfield_12345",
    )
    assert result.stage == "SchemeDecided"
    assert jira.fields("DP-9999")["customfield_12345"] == "SchemeDecided"
    assert jira.current_stage_text("DP-9999") == "SchemeDecided"


def test_lag_marker_posts_readable_prefixed_comment() -> None:
    jira = InMemoryJira()
    payload = JiraLagMarkerInput(
        issue_key="DP-9999",
        stage="ScopeLocked",
        execution_id="exec-1",
        config_root="/tmp/config",
        reason="配置根目录缺少 Jira 配置文件 jira.toml",
    )
    mark_mirror_lag(jira, payload)
    comments = jira.comments("DP-9999")
    assert len(comments) == 1
    assert comments[0].body.startswith(LAG_MARKER_PREFIX)
    assert "镜像滞后" in comments[0].body or comments[0].body.startswith(LAG_MARKER_PREFIX)
    assert "ScopeLocked" in comments[0].body
    assert "Temporal History" in comments[0].body


def test_lag_marker_for_same_execution_and_stage_is_idempotent() -> None:
    jira = InMemoryJira()
    payload = JiraLagMarkerInput(
        issue_key="DP-9999",
        stage="ScopeLocked",
        execution_id="exec-1",
        config_root="/tmp/config",
        reason="Jira 暂时不可用",
    )
    mark_mirror_lag(jira, payload)
    mark_mirror_lag(jira, payload)
    assert len(jira.comments("DP-9999")) == 1


def test_in_memory_client_does_not_store_tokens() -> None:
    jira = InMemoryJira()
    mirror_stage(jira, _stage_input())
    dumped = jira.dump()
    assert "token" not in dumped.lower()
    assert "Bearer" not in dumped
