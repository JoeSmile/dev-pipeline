"""HTTP client tests for the Jira mirror, using httpx MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from dev_pipeline.errors import JiraMirrorError
from dev_pipeline.jira_client import JiraHttpStore, adf_to_text, text_to_adf
from dev_pipeline.jira_config import JiraConfig
from dev_pipeline.jira_mirror import (
    JiraLagMarkerInput,
    JiraStageMirrorInput,
    lag_comment_body,
    mark_mirror_lag,
    mirror_stage,
    stage_comment_body,
)

_CONFIG = JiraConfig(
    base_url="https://example.atlassian.net",
    email="bot@example.com",
    api_token="secret-token-value",
    stage_field=None,
)


def _issue_input(stage: str = "ScopeLocked") -> JiraStageMirrorInput:
    return JiraStageMirrorInput(
        issue_key="DP-9999",
        stage=stage,
        execution_id="exec-1",
        config_root="/tmp/config",
    )


def _client(handler: object) -> httpx.Client:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.Client(
        base_url=_CONFIG.base_url,
        transport=transport,
        auth=(_CONFIG.email, _CONFIG.api_token),
        timeout=15.0,
    )


def test_text_to_adf_and_back_roundtrip() -> None:
    body = stage_comment_body("ScopeLocked", "exec-1")
    assert adf_to_text(text_to_adf(body)) == body


def test_http_store_creates_then_updates_comment() -> None:
    comments: list[dict[str, Any]] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        auth = request.headers.get("authorization", "")
        assert "secret-token-value" not in request.content.decode("utf-8")
        assert auth.startswith("Basic ")
        if request.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        if request.method == "POST":
            payload = json.loads(request.content)
            comment = {"id": "100", "body": payload["body"]}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        if request.method == "PUT" and "/comment/100" in request.url.path:
            payload = json.loads(request.content)
            comments[0] = {"id": "100", "body": payload["body"]}
            return httpx.Response(200, json=comments[0])
        return httpx.Response(500, json={"error": "unexpected"})

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    first = mirror_stage(store, _issue_input("Intake"))
    second = mirror_stage(store, _issue_input("ScopeLocked"))
    assert first.comment_updated is False
    assert second.comment_updated is True
    assert adf_to_text(comments[0]["body"]) == stage_comment_body("ScopeLocked", "exec-1")
    assert any(item.startswith("POST ") for item in calls)
    assert any(item.startswith("PUT ") for item in calls)
    assert len(comments) == 1


def test_http_store_writes_optional_field() -> None:
    fields: dict[str, str] = {}
    comments: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/comment"):
            return httpx.Response(200, json={"comments": comments})
        if request.method == "POST" and request.url.path.endswith("/comment"):
            payload = json.loads(request.content)
            comment = {"id": "1", "body": payload["body"]}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        if request.method == "PUT" and request.url.path.endswith("/issue/DP-9999"):
            payload = json.loads(request.content)
            fields.update(payload["fields"])
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})

    config = JiraConfig(
        base_url=_CONFIG.base_url,
        email=_CONFIG.email,
        api_token=_CONFIG.api_token,
        stage_field="customfield_12345",
    )
    store = JiraHttpStore(config, client=_client(handler))
    result = mirror_stage(store, _issue_input("Retro"), stage_field=config.stage_field)
    assert result.field_updated is True
    assert fields["customfield_12345"] == "Retro"


def test_http_404_fails_in_chinese_without_leaking_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    with pytest.raises(JiraMirrorError, match="不存在") as exc:
        store.list_comments("DP-9999")
    message = str(exc.value)
    assert "DP-9999" in message
    assert "secret-token-value" not in message
    assert "Issue does not exist" not in message


def test_http_network_error_is_chinese_and_has_no_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    with pytest.raises(JiraMirrorError, match="无法连接") as exc:
        store.list_comments("DP-9999")
    assert "secret-token-value" not in str(exc.value)


def test_http_lag_marker_posts_prefixed_comment() -> None:
    comments: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        if request.method == "POST":
            payload = json.loads(request.content)
            comment = {"id": "7", "body": payload["body"]}
            comments.append(comment)
            return httpx.Response(201, json=comment)
        return httpx.Response(500)

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    mark_mirror_lag(
        store,
        JiraLagMarkerInput(
            issue_key="DP-9999",
            stage="ScopeLocked",
            execution_id="exec-1",
            config_root="/tmp/config",
            reason="Jira 暂时不可用",
        ),
    )
    assert adf_to_text(comments[0]["body"]) == lag_comment_body(
        "ScopeLocked",
        "exec-1",
        "Jira 暂时不可用",
    )


def test_http_store_paginates_comments_to_find_existing_mirror() -> None:
    first_page = {
        "startAt": 0,
        "maxResults": 1,
        "total": 2,
        "isLast": False,
        "comments": [{"id": "1", "body": text_to_adf("unrelated comment")}],
    }
    second_page = {
        "startAt": 1,
        "maxResults": 1,
        "total": 2,
        "isLast": True,
        "comments": [{"id": "2", "body": text_to_adf(stage_comment_body("Intake", "exec-1"))}],
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "GET":
            start = int(request.url.params.get("startAt", "0"))
            return httpx.Response(200, json=first_page if start == 0 else second_page)
        if request.method == "PUT" and "/comment/2" in request.url.path:
            return httpx.Response(200, json={"id": "2"})
        return httpx.Response(500, json={"error": "unexpected"})

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    result = mirror_stage(store, _issue_input("ScopeLocked"))
    assert result.comment_updated is True
    assert any("startAt=1" in url for url in calls)


def test_http_store_finds_existing_ticket_and_creates_once() -> None:
    from dev_pipeline.models import TicketSpec
    from dev_pipeline.ticket_list import (
        JiraTicketPersistInput,
        persist_tickets,
        ticket_idempotency_marker,
        ticket_issue_description,
        ticket_issue_summary,
    )

    issues: list[dict[str, Any]] = []
    creates: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/search"):
            jql = request.url.params.get("jql", "")
            matched = [{"key": item["key"]} for item in issues if item["marker"] in jql]
            return httpx.Response(200, json={"issues": matched})
        if request.method == "POST" and request.url.path.endswith("/issue"):
            payload = json.loads(request.content)
            fields = payload["fields"]
            key = f"DP-{len(issues) + 1}"
            description = fields["description"]
            # ADF body is searched via marker stored alongside
            from dev_pipeline.jira_client import adf_to_text

            text = adf_to_text(description)
            issues.append({"key": key, "marker": "dp-ticket:", "body": text})
            # extract exact marker
            for line in text.splitlines():
                if line.startswith("marker: "):
                    issues[-1]["marker"] = line.removeprefix("marker: ")
            creates.append(key)
            return httpx.Response(201, json={"key": key})
        return httpx.Response(500, json={"error": "unexpected"})

    store = JiraHttpStore(_CONFIG, client=_client(handler))
    spec = TicketSpec(
        ticket_id="T-1",
        goal="实现接口",
        acceptance="单测通过",
        io_contract="输入/输出",
    )
    payload = JiraTicketPersistInput(
        story_id="STORY-1",
        execution_id="exec-1",
        config_root="/tmp/config",
        tickets=[spec],
    )
    first = persist_tickets(store, payload)
    second = persist_tickets(store, payload)
    assert first.created == 1
    assert second.created == 0
    assert second.reused == 1
    assert first.tickets[0].jira_key == second.tickets[0].jira_key == "DP-1"
    assert creates == ["DP-1"]
    assert ticket_idempotency_marker("STORY-1", "T-1", "exec-1") in issues[0]["body"]
    assert ticket_issue_summary(spec).startswith("【DevPipeline Ticket】")
    assert "I/O 契约" in ticket_issue_description(spec, story_id="STORY-1", execution_id="exec-1")
