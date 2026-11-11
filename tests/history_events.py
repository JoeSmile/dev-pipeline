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


def workflow_execution_started_count(history: WorkflowHistory) -> int:
    """Return how many WorkflowExecutionStarted events are in the history."""

    return sum(
        1
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
    )



def initiated_child_workflow_ids(history: WorkflowHistory) -> list[str]:
    """Return child workflow ids from start-initiated events, in history order."""
    ids: list[str] = []
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
            continue
        attrs = event.start_child_workflow_execution_initiated_event_attributes
        ids.append(attrs.workflow_id)
    return ids


def started_child_workflow_ids(history: WorkflowHistory) -> list[str]:
    """Return child workflow ids that actually started, in history order."""
    ids: list[str] = []
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
            continue
        execution = event.child_workflow_execution_started_event_attributes.workflow_execution
        ids.append(execution.workflow_id)
    return ids


def child_id_reuse_policies(history: WorkflowHistory) -> list[int]:
    """Return workflow_id_reuse_policy values from child start-initiated events."""
    policies: list[int] = []
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
            continue
        attrs = event.start_child_workflow_execution_initiated_event_attributes
        policies.append(int(attrs.workflow_id_reuse_policy))
    return policies
