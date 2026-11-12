"""Shared CLI helpers for locating a parent execution."""

from __future__ import annotations

from dev_pipeline.intake import validate_jira_key
from dev_pipeline.models import story_workflow_id

_NEED_ID = "请传入 --workflow-id，或同时传入 --jira-key 与 --execution-id，以便定位父执行。"
_MISSING_CONFIG_ROOT = (
    "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 --config-root。"
)


def optional_text(value: object) -> str | None:
    """Return a stripped string, or None when the value is missing/blank."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_parent_workflow_id(
    *,
    workflow_id: str | None,
    jira_key: str | None,
    execution_id: str | None,
) -> str:
    """Resolve the parent workflow id from flags. Does not start a workflow."""

    if workflow_id:
        return workflow_id
    if jira_key is None or execution_id is None:
        raise ValueError(_NEED_ID)
    return story_workflow_id(validate_jira_key(jira_key), execution_id)


__all__ = [
    "optional_text",
    "resolve_parent_workflow_id",
    "_NEED_ID",
    "_MISSING_CONFIG_ROOT",
]
