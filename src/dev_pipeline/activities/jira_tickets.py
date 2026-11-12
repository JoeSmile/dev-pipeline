"""Activity that persists the complete ticket list to Jira project DP."""

from __future__ import annotations

from typing import NoReturn

from temporalio import activity
from temporalio.exceptions import ApplicationError

from dev_pipeline.errors import JiraConfigError, JiraMirrorError
from dev_pipeline.jira_client import JiraHttpStore
from dev_pipeline.jira_config import load_jira_config_from_path
from dev_pipeline.ticket_list import (
    JiraTicketPersistInput,
    JiraTicketPersistResult,
    persist_tickets,
)


def _reraise(exc: Exception, *, retryable: bool) -> NoReturn:
    raise ApplicationError(str(exc), non_retryable=not retryable) from exc


def _retryable_write_error(exc: JiraMirrorError) -> bool:
    message = str(exc)
    return "无法连接" in message or "HTTP 5" in message or "HTTP 429" in message


@activity.defn
async def persist_jira_tickets(payload: JiraTicketPersistInput) -> JiraTicketPersistResult:
    """Idempotently write each required ticket to Jira project DP."""

    try:
        config = load_jira_config_from_path(payload.config_root)
        with JiraHttpStore(config) as store:
            return persist_tickets(store, payload)
    except JiraConfigError as exc:
        _reraise(exc, retryable=False)
    except JiraMirrorError as exc:
        _reraise(exc, retryable=_retryable_write_error(exc))
