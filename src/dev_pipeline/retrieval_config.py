"""Load optional rag.toml from the config-root. This module performs I/O.

Workflow modules must not import this file. Call it from the
``retrieve_scheme_knowledge`` activity only.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dev_pipeline.errors import RetrievalConfigError
from dev_pipeline.retrieval import ALLOWED_CATEGORIES, DEFAULT_MIN_SCORE, PENDING_CORPUS_ROOT

RAG_FILENAME = "rag.toml"
_ALLOWED_TOP_LEVEL = frozenset({"categories", "min_score", "corpus_root"})
_UNREADABLE = "检索配置无法读取: {path}（{exc}）"
_UNPARSABLE = "检索配置无法解析: {path}（{exc}）"
_UNKNOWN_KEY = "检索配置包含未知字段: {key}。"
_NOT_TABLE = "检索配置必须是表。"
_BAD_MIN = "检索配置 min_score 必须是大于或等于 0 的整数，已拒绝: {value}"
_BAD_CATEGORIES = "检索配置 categories 必须是字符串数组。"
_BAD_CORPUS = "检索配置 corpus_root 必须是非空字符串。"
_OUTSIDE = "检索语料根目录必须位于配置根目录内，已拒绝: {path}"


@dataclass(frozen=True)
class RetrievalConfig:
    """Optional scheme-phase retrieval settings loaded from rag.toml."""

    categories: tuple[str, ...] = ALLOWED_CATEGORIES
    min_score: int = DEFAULT_MIN_SCORE
    corpus_root: str = PENDING_CORPUS_ROOT
    source: str = "待证默认"
    configured: bool = False


def load_retrieval_config_from_path(config_root: str) -> RetrievalConfig:
    """Return rag.toml settings, or an unconfigured 待证 default when missing.

    A missing file is not an error: the scheme path stays a marked stub.
    A present but invalid file fails visibly.
    """

    path = Path(config_root) / RAG_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise RetrievalConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        return RetrievalConfig()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RetrievalConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise RetrievalConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    if not isinstance(data, dict):
        raise RetrievalConfigError(_NOT_TABLE)
    return parse_retrieval_config(data, source=str(path), config_root=config_root)


def parse_retrieval_config(
    data: dict[str, Any],
    *,
    source: str,
    config_root: str,
) -> RetrievalConfig:
    """Validate a parsed rag.toml table and resolve the corpus root."""

    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise RetrievalConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    categories = _categories(data)
    min_score = _min_score(data)
    corpus_root = _corpus_root(data, config_root=config_root)
    return RetrievalConfig(
        categories=categories,
        min_score=min_score,
        corpus_root=corpus_root,
        source=source,
        configured=True,
    )


def _categories(data: dict[str, Any]) -> tuple[str, ...]:
    if "categories" not in data:
        return ALLOWED_CATEGORIES
    value = data["categories"]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RetrievalConfigError(_BAD_CATEGORIES)
    cleaned = tuple(item.strip() for item in value if item.strip())
    if not cleaned:
        raise RetrievalConfigError(_BAD_CATEGORIES)
    return cleaned


def _min_score(data: dict[str, Any]) -> int:
    if "min_score" not in data:
        return DEFAULT_MIN_SCORE
    value = data["min_score"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetrievalConfigError(_BAD_MIN.format(value=value))
    return value


def _corpus_root(data: dict[str, Any], *, config_root: str) -> str:
    if "corpus_root" not in data or data["corpus_root"] is None:
        return PENDING_CORPUS_ROOT
    value = data["corpus_root"]
    if not isinstance(value, str) or not value.strip():
        raise RetrievalConfigError(_BAD_CORPUS)
    raw = value.strip()
    if raw == PENDING_CORPUS_ROOT:
        return PENDING_CORPUS_ROOT
    root = Path(config_root).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise RetrievalConfigError(_UNREADABLE.format(path=candidate, exc=exc)) from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise RetrievalConfigError(_OUTSIDE.format(path=resolved))
    return str(resolved)
