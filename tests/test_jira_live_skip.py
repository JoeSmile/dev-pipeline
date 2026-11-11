"""Skip rules for a real Jira fixture probe. This is not an asyncio test."""

from __future__ import annotations

import os

import pytest

_FORBIDDEN_LIVE_KEYS = frozenset({"DP-14"})


def test_live_jira_fixture_skips_without_safe_issue_and_config() -> None:
    """Do not probe a real project ticket. Skip cleanly when no fixture exists."""

    key = os.environ.get("DEV_PIPELINE_JIRA_FIXTURE_KEY", "").strip()
    root = os.environ.get("DEV_PIPELINE_JIRA_CONFIG_ROOT", "").strip()
    if not key or not root:
        pytest.skip(
            "没有安全的一次性 Jira 夹具票或配置根目录，跳过真实 Jira 探测。"
            "可设置 DEV_PIPELINE_JIRA_FIXTURE_KEY 与 DEV_PIPELINE_JIRA_CONFIG_ROOT。"
        )
    if key in _FORBIDDEN_LIVE_KEYS:
        pytest.skip(f"禁止使用 {key} 作为现场镜像探测目标。")
    pytest.skip("本票不在真实项目票上写评论；夹具已提供但仍跳过现场探测。")
