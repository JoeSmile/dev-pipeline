"""Load and validate jira.toml from the config-root. This module performs I/O.

Workflow modules must not import this file. Call it from the Jira mirror
activities only. Tokens stay in the config-root; they are never logged here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dev_pipeline.errors import JiraConfigError

JIRA_FILENAME = "jira.toml"
_ALLOWED_TOP_LEVEL = frozenset({"base_url", "email", "api_token_file", "stage_field"})
_MISSING = "配置根目录缺少 Jira 配置文件 jira.toml: {path}。不会把镜像当成成功。"
_UNREADABLE = "Jira 配置无法读取: {path}（{exc}）"
_UNPARSABLE = "Jira 配置无法解析: {path}（{exc}）"
_UNKNOWN_KEY = "Jira 配置包含未知字段: {key}。"
_MISSING_FIELD = "Jira 配置缺少必填字段: {field}。"
_BAD_URL = "Jira 配置的 base_url 必须是 https 地址，已拒绝: {value}"
_BAD_EMAIL = "Jira 配置的 email 不能为空。"
_BAD_TOKEN_NAME = "Jira 令牌文件必须是配置根目录下的文件名，禁止路径穿越: {value}"
_MISSING_TOKEN = "Jira 令牌文件不存在: {path}"
_EMPTY_TOKEN = "Jira 令牌文件为空: {path}"
_BAD_FIELD = "Jira 配置的 stage_field 不能为空。"


@dataclass(frozen=True)
class JiraConfig:
    """Loaded Jira mirror settings. The token is never written to History."""

    base_url: str
    email: str
    api_token: str
    stage_field: str | None = None


def load_jira_config_from_path(config_root: str) -> JiraConfig:
    """Return Jira settings from ``<config_root>/jira.toml``, or fail visibly."""

    path = Path(config_root) / JIRA_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise JiraConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        raise JiraConfigError(_MISSING.format(path=path))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JiraConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise JiraConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    parsed = parse_jira_config(data)
    token = _read_token(Path(config_root), data["api_token_file"])
    return JiraConfig(
        base_url=parsed.base_url,
        email=parsed.email,
        api_token=token,
        stage_field=parsed.stage_field,
    )


def parse_jira_config(data: dict[str, Any]) -> JiraConfig:
    """Validate a parsed Jira table. The returned token is empty until load."""

    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise JiraConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    base_url = _parse_base_url(data.get("base_url"))
    email = _parse_required_string("email", data.get("email"), _BAD_EMAIL)
    token_name = _parse_required_string(
        "api_token_file",
        data.get("api_token_file"),
        _MISSING_FIELD.format(field="api_token_file"),
    )
    _assert_token_filename(token_name)
    stage_field = data.get("stage_field")
    if stage_field is not None:
        if not isinstance(stage_field, str) or not stage_field.strip():
            raise JiraConfigError(_BAD_FIELD)
        stage_field = stage_field.strip()
    return JiraConfig(
        base_url=base_url,
        email=email,
        api_token="",
        stage_field=stage_field,
    )


def _parse_base_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise JiraConfigError(_MISSING_FIELD.format(field="base_url"))
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise JiraConfigError(_BAD_URL.format(value=raw))
    return value


def _parse_required_string(field: str, raw: object, empty_message: str) -> str:
    if raw is None:
        raise JiraConfigError(_MISSING_FIELD.format(field=field))
    if not isinstance(raw, str) or not raw.strip():
        raise JiraConfigError(empty_message)
    return raw.strip()


def _assert_token_filename(name: str) -> None:
    if Path(name).name != name or name in {".", ".."}:
        raise JiraConfigError(_BAD_TOKEN_NAME.format(value=name))


def _read_token(config_root: Path, name: str) -> str:
    _assert_token_filename(str(name).strip())
    path = config_root / str(name).strip()
    try:
        exists = path.is_file()
    except OSError as exc:
        raise JiraConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        raise JiraConfigError(_MISSING_TOKEN.format(path=path))
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise JiraConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not token:
        raise JiraConfigError(_EMPTY_TOKEN.format(path=path))
    return token
