"""Helpers for asserting Temporal History events in workflow tests."""

from __future__ import annotations

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory


def signaled_names(history: WorkflowHistory) -> list[str]:
    """Return signal names in history order."""
    names: list[str] = []
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            continue
        names.append(event.workflow_execution_signaled_event_attributes.signal_name)
    return names


def scheduled_activity_names(history: WorkflowHistory) -> list[str]:
    """Return scheduled activity type names in history order."""
    names: list[str] = []
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        names.append(event.activity_task_scheduled_event_attributes.activity_type.name)
    return names


