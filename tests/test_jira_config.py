"""Unit tests for loading jira.toml from the config-root."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.errors import JiraConfigError
from dev_pipeline.jira_config import load_jira_config_from_path, parse_jira_config


def _write_jira(
    root: Path,
    *,
    body: str | None = None,
    token: str = "token-value",
    token_name: str = "jira-api-token",
) -> Path:
    if body is None:
        body = (
            'base_url = "https://example.atlassian.net"\n'
            'email = "bot@example.com"\n'
            f'api_token_file = "{token_name}"\n'
        )
    (root / "jira.toml").write_text(body, encoding="utf-8")
    if token_name:
        (root / token_name).write_text(token, encoding="utf-8")
    return root


def test_load_jira_config_reads_required_fields_and_token(tmp_path: Path) -> None:
    root = _write_jira(tmp_path)
    config = load_jira_config_from_path(str(root))
    assert config.base_url == "https://example.atlassian.net"
    assert config.email == "bot@example.com"
    assert config.api_token == "token-value"
    assert config.stage_field is None


def test_load_jira_config_strips_trailing_slash_and_optional_field(tmp_path: Path) -> None:
    _write_jira(
        tmp_path,
        body=(
            'base_url = "https://example.atlassian.net/"\n'
            'email = "bot@example.com"\n'
            'api_token_file = "jira-api-token"\n'
            'stage_field = "customfield_12345"\n'
        ),
    )
    config = load_jira_config_from_path(str(tmp_path))
    assert config.base_url == "https://example.atlassian.net"
    assert config.stage_field == "customfield_12345"


def test_missing_jira_toml_fails_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(JiraConfigError, match="jira.toml") as exc:
        load_jira_config_from_path(str(tmp_path))
    assert "缺少" in str(exc.value)


def test_unparsable_jira_toml_fails_in_chinese(tmp_path: Path) -> None:
    (tmp_path / "jira.toml").write_text("this is not = toml [[[", encoding="utf-8")
    with pytest.raises(JiraConfigError, match="无法解析") as exc:
        load_jira_config_from_path(str(tmp_path))
    assert "jira.toml" in str(exc.value)


def test_missing_token_file_fails_in_chinese(tmp_path: Path) -> None:
    (tmp_path / "jira.toml").write_text(
        'base_url = "https://example.atlassian.net"\n'
        'email = "bot@example.com"\n'
        'api_token_file = "jira-api-token"\n',
        encoding="utf-8",
    )
    with pytest.raises(JiraConfigError, match="令牌") as exc:
        load_jira_config_from_path(str(tmp_path))
    assert "jira-api-token" in str(exc.value)


def test_empty_token_file_fails_in_chinese(tmp_path: Path) -> None:
    _write_jira(tmp_path, token="   ")
    with pytest.raises(JiraConfigError, match="令牌"):
        load_jira_config_from_path(str(tmp_path))


def test_token_file_must_be_a_config_root_filename(tmp_path: Path) -> None:
    (tmp_path / "jira.toml").write_text(
        'base_url = "https://example.atlassian.net"\n'
        'email = "bot@example.com"\n'
        'api_token_file = "../secrets/token"\n',
        encoding="utf-8",
    )
    with pytest.raises(JiraConfigError, match="路径"):
        load_jira_config_from_path(str(tmp_path))


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "base_url"),
        (
            {"base_url": "https://example.atlassian.net", "email": "a@b.com"},
            "api_token_file",
        ),
        (
            {
                "base_url": "ftp://example.atlassian.net",
                "email": "a@b.com",
                "api_token_file": "jira-api-token",
            },
            "base_url",
        ),
        (
            {
                "base_url": "https://example.atlassian.net",
                "email": "a@b.com",
                "api_token_file": "jira-api-token",
                "extra": "nope",
            },
            "未知字段",
        ),
    ],
)
def test_parse_jira_config_rejects_invalid_tables(data: dict[str, object], message: str) -> None:
    with pytest.raises(JiraConfigError, match=message):
        parse_jira_config(data)
