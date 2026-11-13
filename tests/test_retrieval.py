"""Unit tests for the DP-33 scheme-phase RAG retrieval boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_pipeline.errors import RetrievalConfigError
from dev_pipeline.retrieval import (
    FAKE_SUCCESS_CLAIM,
    RETRIEVAL_FAILED,
    RETRIEVAL_NO_HIT,
    RETRIEVAL_OVER_BUDGET,
    RETRIEVAL_STUB,
    RETRIEVAL_SUCCESS,
    RetrievalHit,
    SchemeRetrievalInput,
    SchemeRetrievalRecord,
    build_scheme_query,
    claims_successful_hit,
    evidence_summary,
    is_valid_hit,
    make_record,
)
from dev_pipeline.retrieval_backend import retrieve_scheme_knowledge_from_path
from dev_pipeline.retrieval_config import load_retrieval_config_from_path, parse_retrieval_config


def _hit(**overrides: object) -> RetrievalHit:
    values: dict[str, object] = {
        "doc_id": "std-sso",
        "category": "规范",
        "title": "单点登录规范",
        "excerpt": "登录必须走统一身份认证",
        "score": 5,
    }
    values.update(overrides)
    return RetrievalHit(**values)  # type: ignore[arg-type]


def _write_corpus(config_root: Path, documents: list[dict[str, object]]) -> None:
    corpus = config_root / "scheme-rag"
    corpus.mkdir()
    (corpus / "corpus.json").write_text(
        json.dumps({"documents": documents}, ensure_ascii=False),
        encoding="utf-8",
    )
    (config_root / "rag.toml").write_text(
        "\n".join(
            [
                'categories = ["规范", "历史事故"]',
                "min_score = 1",
                'corpus_root = "scheme-rag"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_valid_hit_requires_scope_score_and_excerpt() -> None:
    assert is_valid_hit(_hit()) is True
    assert is_valid_hit(_hit(category="候选知识")) is False
    assert is_valid_hit(_hit(score=0)) is False
    assert is_valid_hit(_hit(excerpt="")) is False
    assert is_valid_hit(_hit(title="")) is False


def test_evidence_summary_does_not_fake_success_on_degraded_paths() -> None:
    assert FAKE_SUCCESS_CLAIM in evidence_summary(RETRIEVAL_SUCCESS, hit_count=2)
    for outcome in (RETRIEVAL_NO_HIT, RETRIEVAL_FAILED, RETRIEVAL_OVER_BUDGET, RETRIEVAL_STUB):
        label = evidence_summary(outcome, reason="演示原因")
        assert FAKE_SUCCESS_CLAIM not in label
        assert "真实命中" not in label


def test_make_record_drops_hits_unless_retrieval_succeeded() -> None:
    failed = make_record(
        RETRIEVAL_FAILED,
        source="真实检索",
        query="单点登录",
        hits=[_hit()],
        reason="语料无法读取",
    )
    assert failed.hits == []
    assert claims_successful_hit(failed) is False
    assert FAKE_SUCCESS_CLAIM not in failed.evidence_summary
    success = make_record(
        RETRIEVAL_SUCCESS,
        source="真实检索",
        query="单点登录",
        hits=[_hit()],
    )
    assert len(success.hits) == 1
    assert claims_successful_hit(success) is True


def test_claims_successful_hit_flags_leaked_success_wording() -> None:
    leaked = SchemeRetrievalRecord(
        outcome=RETRIEVAL_NO_HIT,
        source="真实检索",
        query="单点登录",
        evidence_summary="已检索到相关规范",
    )
    assert claims_successful_hit(leaked) is True


def test_build_scheme_query_joins_tradeoff_scope_and_hypotheses() -> None:
    query = build_scheme_query("采用单点登录", ["局部接口可替换"], "登录单点")
    assert "采用单点登录" in query
    assert "局部接口可替换" in query
    assert "登录单点" in query


def test_unconfigured_config_root_stays_marked_stub(tmp_path: Path) -> None:
    config = load_retrieval_config_from_path(str(tmp_path))
    assert config.configured is False
    result = retrieve_scheme_knowledge_from_path(
        SchemeRetrievalInput(
            story_id="STORY-1",
            execution_id="exec-1",
            config_root=str(tmp_path),
            query="采用单点登录",
        )
    )
    assert result.outcome == RETRIEVAL_STUB
    assert result.source == "桩"
    assert result.has_new_evidence is False
    assert result.usage.calls == 0
    assert result.record.hits == []
    assert claims_successful_hit(result.record) is False


def test_fixture_corpus_success_and_no_hit(tmp_path: Path) -> None:
    _write_corpus(
        tmp_path,
        [
            {
                "id": "std-sso",
                "category": "规范",
                "title": "单点登录规范",
                "excerpt": "登录必须走统一身份认证",
                "keywords": ["单点登录"],
                "score": 5,
            },
            {
                "id": "inc-session",
                "category": "历史事故",
                "title": "会话固定事故",
                "excerpt": "未旋转会话导致越权",
                "keywords": ["会话固定"],
                "score": 4,
            },
            {
                "id": "other-note",
                "category": "候选知识",
                "title": "不得作为命中",
                "excerpt": "单点登录的闲聊笔记",
                "keywords": ["单点登录"],
                "score": 9,
            },
        ],
    )
    success = retrieve_scheme_knowledge_from_path(
        SchemeRetrievalInput(
            story_id="STORY-1",
            execution_id="exec-1",
            config_root=str(tmp_path),
            query="采用单点登录，参考历史事故",
        )
    )
    assert success.outcome == RETRIEVAL_SUCCESS
    assert success.has_new_evidence is True
    assert success.usage.calls == 1
    assert [item.doc_id for item in success.record.hits] == ["std-sso"]
    assert FAKE_SUCCESS_CLAIM in success.record.evidence_summary
    empty = retrieve_scheme_knowledge_from_path(
        SchemeRetrievalInput(
            story_id="STORY-1",
            execution_id="exec-1",
            config_root=str(tmp_path),
            query="只讨论计费对账",
        )
    )
    assert empty.outcome == RETRIEVAL_NO_HIT
    assert empty.has_new_evidence is False
    assert empty.usage.calls == 1
    assert empty.record.hits == []
    assert claims_successful_hit(empty.record) is False
    assert FAKE_SUCCESS_CLAIM not in empty.record.evidence_summary


def test_missing_corpus_is_honest_failure(tmp_path: Path) -> None:
    (tmp_path / "rag.toml").write_text(
        'categories = ["规范", "历史事故"]\nmin_score = 1\ncorpus_root = "scheme-rag"\n',
        encoding="utf-8",
    )
    result = retrieve_scheme_knowledge_from_path(
        SchemeRetrievalInput(
            story_id="STORY-1",
            execution_id="exec-1",
            config_root=str(tmp_path),
            query="采用单点登录",
        )
    )
    assert result.outcome == RETRIEVAL_FAILED
    assert result.has_new_evidence is False
    assert result.record.hits == []
    assert claims_successful_hit(result.record) is False
    assert FAKE_SUCCESS_CLAIM not in result.record.evidence_summary


def test_corpus_outside_config_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    config_root = tmp_path / "config"
    config_root.mkdir()
    with pytest.raises(RetrievalConfigError, match="必须位于配置根目录内"):
        parse_retrieval_config(
            {"corpus_root": str(outside), "min_score": 1},
            source="test",
            config_root=str(config_root),
        )


def test_complete_scheme_accepts_boundary_marks() -> None:
    from dev_pipeline.models import SchemeDecision, SchemeHypothesis, is_complete_scheme_decision

    hypotheses = [SchemeHypothesis(statement="局部接口可替换", grade="局部")]
    marks = (
        RETRIEVAL_STUB,
        RETRIEVAL_SUCCESS,
        RETRIEVAL_NO_HIT,
        RETRIEVAL_FAILED,
        RETRIEVAL_OVER_BUDGET,
        "真实命中",
    )
    for mark in marks:
        record = SchemeDecision(
            tradeoff="采用现有方案",
            hypotheses=hypotheses,
            decided_by="郭伟",
            decided_at="2026-11-13T04:00:00Z",
            retrieval=mark,  # type: ignore[arg-type]
        )
        assert is_complete_scheme_decision(record) is True
