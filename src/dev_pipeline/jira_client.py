"""Synchronous Jira Cloud REST client used only by Activities."""

from __future__ import annotations

from typing import Any

import httpx

from dev_pipeline.errors import JiraMirrorError
from dev_pipeline.jira_config import JiraConfig
from dev_pipeline.jira_mirror import JiraComment

HTTP_TIMEOUT = 15.0
_NOT_FOUND = "Jira 工单不存在或不可访问: {key}"
_HTTP_FAILED = "Jira 请求失败（HTTP {status}），未能同步工单 {key}。"
_NETWORK = "无法连接 Jira（{key}）: {exc}"


def text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text as a Jira Cloud Atlassian Document Format body."""

    paragraphs: list[dict[str, Any]] = []
    for line in text.split("\n"):
        content: list[dict[str, Any]] = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_to_text(body: object) -> str:
    """Flatten a Jira comment body (ADF or plain string) to text."""

    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    if body.get("type") == "text":
        return str(body.get("text") or "")
    chunks: list[str] = []
    for index, child in enumerate(body.get("content") or []):
        piece = adf_to_text(child)
        if body.get("type") == "doc" and index:
            chunks.append("\n")
        chunks.append(piece)
    return "".join(chunks)


class JiraHttpStore:
    """Jira Cloud issue store backed by the REST API v3."""

    def __init__(self, config: JiraConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url,
            auth=(config.email, config.api_token),
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        """Close the underlying HTTP client when this store owns it."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> JiraHttpStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def list_comments(self, issue_key: str) -> list[JiraComment]:
        """Return comments on the issue, oldest first, following pagination."""

        result: list[JiraComment] = []
        start_at = 0
        page_size = 50
        for _ in range(20):
            payload = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/comment",
                issue_key,
                params={"startAt": start_at, "maxResults": page_size},
            )
            if not isinstance(payload, dict):
                break
            for item in payload.get("comments", []):
                if not isinstance(item, dict):
                    continue
                result.append(
                    JiraComment(
                        comment_id=str(item.get("id", "")),
                        body=adf_to_text(item.get("body")),
                    )
                )
            raw_comments = payload.get("comments", [])
            fetched = len(raw_comments) if isinstance(raw_comments, list) else 0
            total = payload.get("total")
            is_last = payload.get("isLast")
            start_at += fetched
            if is_last is True or fetched == 0:
                break
            if isinstance(total, int) and start_at >= total:
                break
        return result

    def create_comment(self, issue_key: str, body: str) -> JiraComment:
        """Create a comment with an ADF body."""

        payload = self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            issue_key,
            json={"body": text_to_adf(body)},
        )
        comment_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
        return JiraComment(comment_id=comment_id, body=body)

    def update_comment(self, issue_key: str, comment_id: str, body: str) -> JiraComment:
        """Replace the body of an existing comment."""

        self._request(
            "PUT",
            f"/rest/api/3/issue/{issue_key}/comment/{comment_id}",
            issue_key,
            json={"body": text_to_adf(body)},
        )
        return JiraComment(comment_id=comment_id, body=body)

    def set_field(self, issue_key: str, field_name: str, value: str) -> None:
        """Write a custom field. The field id/name remains 待证 until configured."""

        self._request(
            "PUT",
            f"/rest/api/3/issue/{issue_key}",
            issue_key,
            json={"fields": {field_name: value}},
        )

    def find_issue_key_by_marker(self, marker: str, project_key: str) -> str | None:
        """Search project issues whose description contains the idempotency marker."""

        jql = f'project = {project_key} AND text ~ "{marker}"'
        payload = self._request(
            "GET",
            "/rest/api/3/search",
            marker,
            params={"jql": jql, "fields": "key", "maxResults": "50"},
        )
        if not isinstance(payload, dict):
            return None
        issues = payload.get("issues") or []
        if not isinstance(issues, list):
            return None
        for item in issues:
            if isinstance(item, dict) and item.get("key"):
                return str(item["key"])
        return None

    def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
    ) -> str:
        """Create one issue in the given project and return its key."""

        payload = self._request(
            "POST",
            "/rest/api/3/issue",
            summary,
            json={
                "fields": {
                    "project": {"key": project_key},
                    "issuetype": {"name": issue_type},
                    "summary": summary,
                    "description": text_to_adf(description),
                    "labels": labels,
                }
            },
        )
        if not isinstance(payload, dict) or not payload.get("key"):
            raise JiraMirrorError(f"Jira 建票响应缺少 key（{summary}）")
        return str(payload["key"])

    def _request(
        self,
        method: str,
        path: str,
        issue_key: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | dict[str, int] | None = None,
    ) -> object:
        try:
            response = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise JiraMirrorError(_NETWORK.format(key=issue_key, exc=type(exc).__name__)) from exc
        if response.status_code == 404:
            raise JiraMirrorError(_NOT_FOUND.format(key=issue_key))
        if response.status_code >= 400:
            raise JiraMirrorError(_HTTP_FAILED.format(status=response.status_code, key=issue_key))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}
