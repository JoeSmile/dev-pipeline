"""Shared pytest fixtures for table tests and WorkflowEnvironment tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from dev_pipeline.constants import DEFAULT_TASK_QUEUE


@pytest.fixture
def tmp_config_root(tmp_path: Path) -> Path:
    root = tmp_path / "config-root"
    root.mkdir()
    return root


@pytest.fixture
def task_queue() -> Iterator[str]:
    yield DEFAULT_TASK_QUEUE


@pytest.fixture
async def workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env
