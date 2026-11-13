"""Fixture or local-file corpus search for scheme-phase RAG. This module does I/O.

Workflow modules must not import this file. Prefer an in-memory or file
fixture corpus; do not call a live commercial RAG API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dev_pipeline.budget import BudgetAmounts
from dev_pipeline.errors import RetrievalConfigError
from dev_pipeline.retrieval import (
    PENDING_CORPUS_ROOT,
    RETRIEVAL_FAILED,
    RETRIEVAL_NO_HIT,
    RETRIEVAL_SOURCE_REAL,
    RETRIEVAL_SOURCE_STUB,
    RETRIEVAL_STUB,
    RETRIEVAL_SUCCESS,
    RetrievalHit,
    SchemeRetrievalInput,
    SchemeRetrievalResult,
    is_valid_hit,
    make_record,
    result_from_record,
)
from dev_pipeline.retrieval_config import load_retrieval_config_from_path

_UNREADABLE = "检索语料无法读取: {path}（{exc}）"
_UNPARSABLE = "检索语料无法解析: {path}（{exc}）"
_MISSING_CORPUS = "检索语料根目录待证或语料文件缺失: {path}"
_NOT_CONFIGURED = "语料根目录待证，未配置 rag.toml，未执行真实检索"
_EMPTY_QUERY = "检索查询为空，无法形成有效命中"
_ATTEMPT_USAGE = BudgetAmounts(calls=1)
_READ_LIMIT_BYTES = 1_000_000


def retrieve_scheme_knowledge_from_path(payload: SchemeRetrievalInput) -> SchemeRetrievalResult:
    """Run the scheme-phase retrieval boundary against a local fixture corpus."""

    try:
        config = load_retrieval_config_from_path(payload.config_root)
    except RetrievalConfigError as exc:
        return _failed(payload.query, reason=str(exc), corpus_root=PENDING_CORPUS_ROOT)
    if not config.configured:
        return result_from_record(
            make_record(
                RETRIEVAL_STUB,
                source=RETRIEVAL_SOURCE_STUB,
                query=payload.query,
                reason=_NOT_CONFIGURED,
                usage=BudgetAmounts(),
                corpus_root=PENDING_CORPUS_ROOT,
            )
        )
    if config.corpus_root == PENDING_CORPUS_ROOT:
        return _failed(payload.query, reason=_MISSING_CORPUS.format(path=PENDING_CORPUS_ROOT))
    try:
        documents = load_corpus_documents(config.corpus_root)
    except (OSError, RetrievalConfigError, ValueError, TypeError) as exc:
        reason = str(exc) if str(exc) else _UNREADABLE.format(path=config.corpus_root, exc=exc)
        return _failed(payload.query, reason=reason, corpus_root=config.corpus_root)
    query = payload.query.strip()
    if not query:
        record = make_record(
            RETRIEVAL_NO_HIT,
            source=RETRIEVAL_SOURCE_REAL,
            query=payload.query,
            reason=_EMPTY_QUERY,
            usage=_ATTEMPT_USAGE,
            corpus_root=config.corpus_root,
        )
        return result_from_record(record)
    hits = score_documents(documents, query=query, min_score=config.min_score)
    if not hits:
        record = make_record(
            RETRIEVAL_NO_HIT,
            source=RETRIEVAL_SOURCE_REAL,
            query=query,
            reason="授权语料中没有满足有效命中条件的文档",
            usage=_ATTEMPT_USAGE,
            corpus_root=config.corpus_root,
        )
        return result_from_record(record)
    record = make_record(
        RETRIEVAL_SUCCESS,
        source=RETRIEVAL_SOURCE_REAL,
        query=query,
        hits=hits,
        reason="有效命中来自规范或历史事故",
        usage=_ATTEMPT_USAGE,
        corpus_root=config.corpus_root,
    )
    return result_from_record(record)


def load_corpus_documents(corpus_root: str) -> list[dict[str, Any]]:
    """Load documents from ``corpus.json`` under an authorized corpus root."""

    root = Path(corpus_root)
    try:
        if not root.is_dir():
            raise RetrievalConfigError(_MISSING_CORPUS.format(path=root))
    except OSError as exc:
        raise RetrievalConfigError(_UNREADABLE.format(path=root, exc=exc)) from exc
    path = root / "corpus.json"
    try:
        if not path.is_file():
            raise RetrievalConfigError(_MISSING_CORPUS.format(path=path))
        size = path.stat().st_size
        if size > _READ_LIMIT_BYTES:
            raise RetrievalConfigError(f"检索语料过大，已拒绝: {path}")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RetrievalConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RetrievalConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    documents = data.get("documents", data) if isinstance(data, dict) else data
    if not isinstance(documents, list):
        raise RetrievalConfigError(_UNPARSABLE.format(path=path, exc="documents 必须是数组"))
    return documents


def score_documents(
    documents: list[dict[str, Any]],
    *,
    query: str,
    min_score: int,
) -> list[RetrievalHit]:
    """Return valid hits whose keywords appear in the query."""

    needle = query.casefold()
    hits: list[RetrievalHit] = []
    for item in documents:
        if not isinstance(item, dict):
            continue
        candidate = _candidate_hit(item)
        if candidate is None:
            continue
        keywords = _keywords(item)
        if keywords and not any(keyword.casefold() in needle for keyword in keywords):
            continue
        if not keywords and candidate.title.casefold() not in needle:
            body = str(item.get("body", "")).strip()
            if candidate.title.casefold() not in needle and body.casefold() not in needle:
                continue
        if is_valid_hit(candidate, min_score=min_score):
            hits.append(candidate)
    hits.sort(key=lambda item: (-item.score, item.doc_id))
    return hits


def _candidate_hit(item: dict[str, Any]) -> RetrievalHit | None:
    doc_id = str(item.get("id") or item.get("doc_id") or "").strip()
    category = str(item.get("category") or "").strip()
    title = str(item.get("title") or "").strip()
    excerpt = str(item.get("excerpt") or item.get("body") or "").strip()
    path = str(item.get("path") or "").strip()
    raw_score = item.get("score", 0)
    if isinstance(raw_score, bool) or not isinstance(raw_score, int):
        return None
    if not doc_id:
        return None
    return RetrievalHit(
        doc_id=doc_id,
        category=category,
        title=title,
        excerpt=excerpt,
        score=raw_score,
        path=path,
    )


def _keywords(item: dict[str, Any]) -> list[str]:
    value = item.get("keywords", [])
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _failed(
    query: str,
    *,
    reason: str,
    corpus_root: str = PENDING_CORPUS_ROOT,
) -> SchemeRetrievalResult:
    record = make_record(
        RETRIEVAL_FAILED,
        source=RETRIEVAL_SOURCE_REAL,
        query=query,
        reason=reason,
        usage=_ATTEMPT_USAGE,
        corpus_root=corpus_root,
    )
    return result_from_record(record)
