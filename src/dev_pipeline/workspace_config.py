"""Load optional workspace.toml from the config-root. This module performs I/O.

Workflow modules must not import this file. Call it from the workspace claim
activity and the coding-sandbox path only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from dev_pipeline.errors import WorkspaceConfigError

WORKSPACE_FILENAME = "workspace.toml"
_ALLOWED_TOP_LEVEL = frozenset({"authorized_root", "knowledge_root"})
_UNREADABLE = "工作区配置无法读取: {path}（{exc}）"
_UNPARSABLE = "工作区配置无法解析: {path}（{exc}）"
_UNKNOWN_KEY = "工作区配置包含未知字段: {key}。"
_NOT_TABLE = "工作区配置必须是表。"
_BAD_ROOT = "授权工作区根目录 authorized_root 必须是已存在的绝对目录，已拒绝: {value}"
_NOT_ABSOLUTE = "授权工作区根目录必须是绝对路径，已拒绝相对路径: {path}"
_MISSING_PATH = "授权工作区根目录不存在: {path}"
_NOT_DIR = "授权工作区根目录不是目录: {path}"
_UNREACHABLE = "授权工作区根目录无法访问: {path}（{exc}）"


def load_authorized_root_from_path(config_root: str | None) -> str:
    """Return the authorized root from ``<config_root>/workspace.toml``, or empty.

    A missing file is not an error: the coding sandbox stays unauthorized and
    must reject writes. A present but invalid file fails visibly.
    An empty or missing ``config_root`` is treated as unauthorized, not as a
    hidden default directory.
    """

    if config_root is None or not str(config_root).strip():
        return ""
    path = Path(str(config_root).strip()) / WORKSPACE_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise WorkspaceConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    if not isinstance(data, dict):
        raise WorkspaceConfigError(_NOT_TABLE)
    return parse_authorized_root(data)


def parse_authorized_root(data: dict[str, Any]) -> str:
    """Validate a parsed workspace table and return the authorized root path."""

    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise WorkspaceConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    if "authorized_root" not in data:
        return ""
    value = data["authorized_root"]
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceConfigError(_BAD_ROOT.format(value=value))
    raw = value.strip()
    root = Path(raw)
    if not root.is_absolute():
        raise WorkspaceConfigError(_NOT_ABSOLUTE.format(path=raw))
    try:
        exists = root.exists()
        is_dir = root.is_dir()
    except OSError as exc:
        raise WorkspaceConfigError(_UNREACHABLE.format(path=raw, exc=exc)) from exc
    if not exists:
        raise WorkspaceConfigError(_MISSING_PATH.format(path=raw))
    if not is_dir:
        raise WorkspaceConfigError(_NOT_DIR.format(path=raw))
    return str(root)


_BAD_KNOWLEDGE = "知识根目录 knowledge_root 必须是已存在的绝对目录，已拒绝: {value}"
_KNOWLEDGE_NOT_ABSOLUTE = "知识根目录必须是绝对路径，已拒绝相对路径: {path}"
_KNOWLEDGE_MISSING = "知识根目录不存在: {path}"
_KNOWLEDGE_NOT_DIR = "知识根目录不是目录: {path}"
_KNOWLEDGE_UNREACHABLE = "知识根目录无法访问: {path}（{exc}）"


def parse_knowledge_root(data: dict[str, Any]) -> str:
    """Return knowledge_root from a parsed workspace table, or empty when omitted."""

    if "knowledge_root" not in data:
        return ""
    value = data["knowledge_root"]
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceConfigError(_BAD_KNOWLEDGE.format(value=value))
    raw = value.strip()
    root = Path(raw)
    if not root.is_absolute():
        raise WorkspaceConfigError(_KNOWLEDGE_NOT_ABSOLUTE.format(path=raw))
    try:
        exists = root.exists()
        is_dir = root.is_dir()
    except OSError as exc:
        raise WorkspaceConfigError(_KNOWLEDGE_UNREACHABLE.format(path=raw, exc=exc)) from exc
    if not exists:
        raise WorkspaceConfigError(_KNOWLEDGE_MISSING.format(path=raw))
    if not is_dir:
        raise WorkspaceConfigError(_KNOWLEDGE_NOT_DIR.format(path=raw))
    return str(root)


def load_knowledge_root_from_path(config_root: str | None) -> str:
    """Return knowledge_root from workspace.toml, or empty when unset or missing."""

    if config_root is None or not str(config_root).strip():
        return ""
    path = Path(str(config_root).strip()) / WORKSPACE_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise WorkspaceConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    if not isinstance(data, dict):
        raise WorkspaceConfigError(_NOT_TABLE)
    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise WorkspaceConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    return parse_knowledge_root(data)


def resolve_knowledge_root(config_root: str) -> str:
    """Return the directory that holds docs/knowledge for Story Retro writes.

    Preference: explicit knowledge_root, then authorized_root, then config_root.
    Writes never fall back to the process working directory.
    """

    knowledge = load_knowledge_root_from_path(config_root)
    if knowledge:
        return knowledge
    authorized = load_authorized_root_from_path(config_root)
    if authorized:
        return authorized
    return str(Path(str(config_root).strip()))
