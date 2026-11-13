"""WorkflowEnvironment tests for DP-33 scheme-phase retrieval paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import scheme_decision, scope_lock, start_parent, story, wait_snapshot

from dev_pipeline.models import IntakeRecord, StoryInput, TicketSpec
from dev_pipeline.retrieval import FAKE_SUCCESS_CLAIM, claims_successful_hit
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


def _story(config_root: Path) -> StoryInput:
    payload = story(
        config_root,
        [TicketSpec(ticket_id="A")],
        auto_start_children=False,
    )
    payload.intake = IntakeRecord(jira_key="DP-33", request="把登录改成单点登录", source="cli")
    return payload


def _write_corpus(config_root: Path, documents: list[dict[str, object]]) -> None:
    corpus = config_root / "scheme-rag"
    corpus.mkdir()
    (corpus / "corpus.json").write_text(
        json.dumps({"documents": documents}, ensure_ascii=False),
        encoding="utf-8",
    )
    (config_root / "rag.toml").write_text(
        'categories = ["规范", "历史事故"]\nmin_score = 1\ncorpus_root = "scheme-rag"\n',
        encoding="utf-8",
    )


async def _lock_and_decide(
    handle: object,
    payload: StoryInput,
    *,
    tradeoff: str = "采用单点登录，不重写账号库",
) -> object:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.lock_scope,
        scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
    )
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.SCOPE_LOCKED and item.waiting_signal == "decide_scheme"
        ),
    )
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.decide_scheme,
        scheme_decision(payload, decided_by="郭伟", tradeoff=tradeoff),
    )
    return await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.TICKETS_READY
            and item.scheme is not None
            and item.scheme_retrieval is not None
        ),
    )


def _sso_documents() -> list[dict[str, object]]:
    return [
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
    ]


async def test_unconfigured_scheme_retrieval_stays_marked_stub(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "桩"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.is_stub is True
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert snap.budget is not None
        assert snap.budget.spent.calls == 0
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_successful_retrieval_books_budget_and_records_hits(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "检索成功"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.source == "真实检索"
        assert [item.doc_id for item in snap.scheme_retrieval.hits] == ["std-sso"]
        assert FAKE_SUCCESS_CLAIM in snap.scheme_retrieval.evidence_summary
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_no_hit_does_not_claim_success_and_stops_rag_path(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _lock_and_decide(handle, payload, tradeoff="只讨论计费对账，不改登录")
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "无命中"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert FAKE_SUCCESS_CLAIM not in snap.scheme_retrieval.evidence_summary
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        assert "rag" in snap.budget.stopped_paths
        assert "rag" in snap.budget.no_new_evidence_paths
    finally:
        await worker.__aexit__(None, None, None)


async def test_failed_retrieval_does_not_fill_fake_hits(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    (tmp_config_root / "rag.toml").write_text(
        'categories = ["规范", "历史事故"]\nmin_score = 1\ncorpus_root = "scheme-rag"\n',
        encoding="utf-8",
    )
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "检索失败"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert FAKE_SUCCESS_CLAIM not in snap.scheme_retrieval.evidence_summary
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_over_budget_skips_activity_and_does_not_claim_success(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    (tmp_config_root / "budget.toml").write_text(
        'units = ["calls"]\n[limits]\ncalls = 0\n',
        encoding="utf-8",
    )
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "超预算"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert FAKE_SUCCESS_CLAIM not in snap.scheme_retrieval.evidence_summary
        assert snap.budget is not None
        assert snap.budget.spent.calls == 0
        assert snap.budget.last_breach is not None
        assert snap.budget.last_breach.bad == "Bad"
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" not in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)
