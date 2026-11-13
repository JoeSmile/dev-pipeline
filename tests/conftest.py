"""Shared pytest fixtures for table tests and WorkflowEnvironment tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from dev_pipeline.constants import DEFAULT_TASK_QUEUE


def _dev_server_path() -> str | None:
    path = os.environ.get("TEMPORAL_DEV_SERVER_PATH", "").strip()
    return path or None


# 待证占位限额，不是已签批的正式配额。仅供测试夹具使用，可被测试覆盖。
_PENDING_PLACEHOLDER_BUDGET = '''\
# 待证占位限额，不是已签批的正式配额。
units = ["tokens", "calls", "cost"]

[limits]
tokens = 100000
calls = 50
cost = 1000
'''


@pytest.fixture
def tmp_config_root(tmp_path: Path) -> Path:
    root = tmp_path / "config-root"
    root.mkdir()
    (root / "budget.toml").write_text(_PENDING_PLACEHOLDER_BUDGET, encoding="utf-8")
    return root


@pytest.fixture
def task_queue() -> Iterator[str]:
    yield DEFAULT_TASK_QUEUE


@pytest.fixture
async def workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    cli = _dev_server_path()
    if cli:
        # Docker/CI can ship the official CLI so tests never hit temporal.download.
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=cli, ui=False
        ) as env:
            yield env
        return
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@pytest.fixture(autouse=True)
def _reset_remote_write_journal() -> Iterator[None]:
    from dev_pipeline.remote_write_store import reset_recorded_rejections

    reset_recorded_rejections()
    yield
    reset_recorded_rejections()
