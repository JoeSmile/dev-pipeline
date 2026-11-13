"""Live Temporal History proof for DP-33 scheme-phase RAG retrieval.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a fixture corpus only: no live commercial RAG, no real Jira writes, and
no live Cursor CLI.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    scheme_decision,
    scope_lock,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    IntakeRecord,
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.retrieval import FAKE_SUCCESS_CLAIM, claims_successful_hit
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


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


def _sso_documents() -> list[dict[str, object]]:
    return [
        {
            "id": "std-sso",
            "category": "规范",
            "title": "单点登录规范",
            "excerpt": "登录必须走统一身份认证",
            "keywords": ["单点登录"],
            "score": 5,
        }
    ]


async def _start_live(
    live_client: Client,
    tmp_config_root: Path,
    suffix: str,
) -> tuple[Worker, object, StoryInput]:
    payload = StoryInput(
        story_id="DP-33",
        execution_id=f"exec-live-rag-{suffix}",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="不要用这段",
        intake=IntakeRecord(
            jira_key="DP-33",
            request="把登录改成单点登录",
            source="cli",
        ),
        auto_start_children=False,
    )
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-rag-live-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload


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
        timeout=30.0,
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
        timeout=30.0,
    )


async def _abort(handle: object) -> None:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.abort,
        AbortRequest(reason="live-rag-stop"),
    )
    await wait_terminal(handle, timeout=30.0)  # type: ignore[arg-type]


async def test_live_successful_retrieval_books_budget(
    live_client: Client, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload = await _start_live(live_client, tmp_config_root, suffix)
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "检索成功"
        assert snap.scheme_retrieval is not None
        assert [item.doc_id for item in snap.scheme_retrieval.hits] == ["std-sso"]
        assert FAKE_SUCCESS_CLAIM in snap.scheme_retrieval.evidence_summary
        assert snap.budget is not None
        assert snap.budget.spent.calls == 1
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" in scheduled_activity_names(history)
        await _abort(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_no_hit_and_failure_do_not_claim_success(
    live_client: Client, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload = await _start_live(live_client, tmp_config_root, f"{suffix}n")
    try:
        snap = await _lock_and_decide(handle, payload, tradeoff="只讨论计费对账，不改登录")
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "无命中"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert FAKE_SUCCESS_CLAIM not in snap.scheme_retrieval.evidence_summary
        await _abort(handle)
    finally:
        await worker.__aexit__(None, None, None)

    fail_root = tmp_config_root / "fail-root"
    fail_root.mkdir()
    (fail_root / "budget.toml").write_text(
        tmp_config_root.joinpath("budget.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (fail_root / "rag.toml").write_text(
        'categories = ["规范", "历史事故"]\nmin_score = 1\ncorpus_root = "scheme-rag"\n',
        encoding="utf-8",
    )
    worker, handle, payload = await _start_live(live_client, fail_root, f"{suffix}f")
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "检索失败"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        await _abort(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_over_budget_does_not_schedule_retrieval(
    live_client: Client, tmp_config_root: Path
) -> None:
    _write_corpus(tmp_config_root, _sso_documents())
    (tmp_config_root / "budget.toml").write_text(
        'units = ["calls"]\n[limits]\ncalls = 0\n',
        encoding="utf-8",
    )
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload = await _start_live(live_client, tmp_config_root, suffix)
    try:
        snap = await _lock_and_decide(handle, payload)
        assert snap.scheme is not None
        assert snap.scheme.retrieval == "超预算"
        assert snap.scheme_retrieval is not None
        assert snap.scheme_retrieval.hits == []
        assert claims_successful_hit(snap.scheme_retrieval) is False
        assert FAKE_SUCCESS_CLAIM not in snap.scheme_retrieval.evidence_summary
        history = await handle.fetch_history()
        assert "retrieve_scheme_knowledge" not in scheduled_activity_names(history)
        await _abort(handle)
    finally:
        await worker.__aexit__(None, None, None)
