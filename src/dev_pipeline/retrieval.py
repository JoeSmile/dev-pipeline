"""Scheme-phase RAG retrieval boundary. Workflows may import this module.

This module performs no I/O and must not read the clock, draw random numbers,
or call external services. Corpus reads and path checks belong in Activities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.budget import BudgetAmounts

RetrievalSource = Literal["桩", "真实检索"]
RetrievalOutcome = Literal["桩", "检索成功", "无命中", "检索失败", "超预算"]
# 真实命中 is the pre-DP-33 success label; accepted on inherited records only.
RetrievalMark = Literal["桩", "真实命中", "检索成功", "无命中", "检索失败", "超预算"]

RETRIEVAL_STUB: RetrievalMark = "桩"
RETRIEVAL_HIT: RetrievalMark = "真实命中"
RETRIEVAL_SUCCESS: RetrievalMark = "检索成功"
RETRIEVAL_NO_HIT: RetrievalMark = "无命中"
RETRIEVAL_FAILED: RetrievalMark = "检索失败"
RETRIEVAL_OVER_BUDGET: RetrievalMark = "超预算"
RETRIEVAL_SOURCE_STUB: RetrievalSource = "桩"
RETRIEVAL_SOURCE_REAL: RetrievalSource = "真实检索"

ALLOWED_CATEGORIES: tuple[str, ...] = ("规范", "历史事故")
# 待证占位：有效命中最低分，不是已签批的正式阈值。
DEFAULT_MIN_SCORE = 1
PENDING_CORPUS_ROOT = "待证"
FAKE_SUCCESS_CLAIM = "已检索到"

RETRIEVAL_MARKS: frozenset[str] = frozenset(
    {
        RETRIEVAL_STUB,
        RETRIEVAL_HIT,
        RETRIEVAL_SUCCESS,
        RETRIEVAL_NO_HIT,
        RETRIEVAL_FAILED,
        RETRIEVAL_OVER_BUDGET,
    }
)


@dataclass
class RetrievalHit:
    """One valid scheme-phase hit inside the allowed retrieval scope."""

    doc_id: str
    category: str
    title: str
    excerpt: str
    score: int
    path: str = ""


@dataclass
class SchemeRetrievalRecord:
    """Honest execution record for one scheme-phase retrieval attempt."""

    outcome: RetrievalMark = RETRIEVAL_STUB
    source: RetrievalSource = RETRIEVAL_SOURCE_STUB
    query: str = ""
    categories: list[str] = field(default_factory=lambda: list(ALLOWED_CATEGORIES))
    hits: list[RetrievalHit] = field(default_factory=list)
    reason: str = ""
    evidence_summary: str = ""
    usage: BudgetAmounts = field(default_factory=BudgetAmounts)
    corpus_root: str = PENDING_CORPUS_ROOT
    is_stub: bool = False


@dataclass
class SchemeRetrievalInput:
    """Activity input for scheme-phase retrieval. Contains no secrets."""

    story_id: str
    execution_id: str
    config_root: str
    query: str


@dataclass
class SchemeRetrievalResult:
    """Activity result: outcome, usage, and the queryable retrieval record."""

    outcome: RetrievalMark
    source: RetrievalSource
    usage: BudgetAmounts = field(default_factory=BudgetAmounts)
    has_new_evidence: bool = False
    record: SchemeRetrievalRecord = field(default_factory=SchemeRetrievalRecord)
    detail: str = ""


def is_allowed_category(category: str) -> bool:
    """Return True when the document category is inside the scheme-phase scope."""

    return category in ALLOWED_CATEGORIES


def is_valid_hit(hit: RetrievalHit, *, min_score: int = DEFAULT_MIN_SCORE) -> bool:
    """Return True when a candidate is a usable 规范 or 历史事故 hit."""

    if not is_allowed_category(hit.category):
        return False
    if not hit.doc_id.strip() or not hit.title.strip() or not hit.excerpt.strip():
        return False
    return hit.score >= min_score


def build_scheme_query(tradeoff: str, hypotheses: list[str], in_scope: str = "") -> str:
    """Join scheme and scope text into one retrieval query string."""

    parts = [tradeoff, in_scope, *hypotheses]
    return " ".join(item.strip() for item in parts if item and item.strip())


def evidence_summary(outcome: RetrievalMark, *, hit_count: int = 0, reason: str = "") -> str:
    """Build an operator-visible evidence label that never fakes a successful hit."""

    if outcome == RETRIEVAL_SUCCESS:
        return f"{FAKE_SUCCESS_CLAIM}{hit_count}条有效命中（规范 / 历史事故）"
    if outcome == RETRIEVAL_NO_HIT:
        return "无命中：已检索授权语料，没有有效命中"
    if outcome == RETRIEVAL_FAILED:
        suffix = f"：{reason}" if reason else ""
        return f"检索失败{suffix}"
    if outcome == RETRIEVAL_OVER_BUDGET:
        suffix = f"：{reason}" if reason else ""
        return f"超预算：未启动检索{suffix}"
    return "桩：未执行真实检索"


def claims_successful_hit(record: SchemeRetrievalRecord) -> bool:
    """Return True when the record pretends a successful search found evidence."""

    if record.outcome in {RETRIEVAL_SUCCESS, RETRIEVAL_HIT} and record.hits:
        return True
    texts = [
        record.outcome,
        record.reason,
        record.evidence_summary,
        *(hit.excerpt for hit in record.hits),
    ]
    blob = " ".join(texts)
    if record.outcome not in {RETRIEVAL_SUCCESS, RETRIEVAL_HIT} and FAKE_SUCCESS_CLAIM in blob:
        return True
    if record.outcome not in {RETRIEVAL_SUCCESS, RETRIEVAL_HIT} and record.hits:
        return True
    return False


def make_record(
    outcome: RetrievalMark,
    *,
    source: RetrievalSource,
    query: str,
    hits: list[RetrievalHit] | None = None,
    reason: str = "",
    usage: BudgetAmounts | None = None,
    corpus_root: str = PENDING_CORPUS_ROOT,
) -> SchemeRetrievalRecord:
    """Assemble an honest retrieval record for the given outcome."""

    validated = list(hits or [])
    if outcome != RETRIEVAL_SUCCESS:
        validated = []
    summary = evidence_summary(outcome, hit_count=len(validated), reason=reason)
    return SchemeRetrievalRecord(
        outcome=outcome,
        source=source,
        query=query,
        categories=list(ALLOWED_CATEGORIES),
        hits=validated,
        reason=reason,
        evidence_summary=summary,
        usage=usage or BudgetAmounts(),
        corpus_root=corpus_root,
        is_stub=source == RETRIEVAL_SOURCE_STUB or outcome == RETRIEVAL_STUB,
    )


def result_from_record(record: SchemeRetrievalRecord) -> SchemeRetrievalResult:
    """Wrap a record into the Activity return type."""

    return SchemeRetrievalResult(
        outcome=record.outcome,
        source=record.source,
        usage=record.usage,
        has_new_evidence=record.outcome == RETRIEVAL_SUCCESS and bool(record.hits),
        record=record,
        detail=record.evidence_summary,
    )
