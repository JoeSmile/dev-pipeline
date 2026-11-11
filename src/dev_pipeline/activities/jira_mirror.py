"""Activities that mirror a parent stage onto a Jira issue."""

from __future__ import annotations

from typing import NoReturn

from temporalio import activity
from temporalio.exceptions import ApplicationError

from dev_pipeline.errors import JiraConfigError, JiraMirrorError
from dev_pipeline.jira_client import JiraHttpStore
from dev_pipeline.jira_config import load_jira_config_from_path
from dev_pipeline.jira_mirror import (
    JiraLagMarkerInput,
    JiraStageMirrorInput,
    JiraStageMirrorResult,
    mark_mirror_lag,
    mirror_stage,
)


def _reraise(exc: Exception, *, retryable: bool) -> NoReturn:
    raise ApplicationError(str(exc), non_retryable=not retryable) from exc


def _retryable_mirror_error(exc: JiraMirrorError) -> bool:
    message = str(exc)
    return "无法连接" in message or "HTTP 5" in message or "HTTP 429" in message


@activity.defn
async def mirror_parent_stage(payload: JiraStageMirrorInput) -> JiraStageMirrorResult:
    """Write the current parent stage to the Jira issue, or fail visibly."""

    try:
        config = load_jira_config_from_path(payload.config_root)
        with JiraHttpStore(config) as store:
            return mirror_stage(store, payload, stage_field=config.stage_field)
    except JiraConfigError as exc:
        _reraise(exc, retryable=False)
    except JiraMirrorError as exc:
        _reraise(exc, retryable=_retryable_mirror_error(exc))


@activity.defn
async def mark_jira_mirror_lag(payload: JiraLagMarkerInput) -> None:
    """Best-effort lag marker so the Jira issue can show that sync did not finish."""

    try:
        config = load_jira_config_from_path(payload.config_root)
        with JiraHttpStore(config) as store:
            mark_mirror_lag(store, payload)
    except JiraConfigError as exc:
        _reraise(exc, retryable=False)
    except JiraMirrorError as exc:
        _reraise(exc, retryable=_retryable_mirror_error(exc))
