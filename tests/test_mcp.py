import base64
import json

import pytest
from starlette.testclient import TestClient

from search import Citation, SearchFailure, SearchResult
from server import create_app


class FakeSearch:
    def __init__(self, fail=False):
        self.queries = []
        self.fail = fail

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise SearchFailure("No grounded answer with citations was returned; refine the query.")
        return SearchResult(
            answer="Public answer",
            citations=[Citation(url="https://example.com", title="Example", start_index=0, end_index=13)],
            response_id="resp_test",
        )


def headers(scopes="search"):
    principal = {"auth_typ": "aad", "claims": [{"typ": "scp", "val": scopes}]}
    return {
        "Accept": "application/json, text/event-stream",
        "x-ms-client-principal": base64.b64encode(json.dumps(principal).encode()).decode(),
    }


def rpc(client, method, params, request_headers=None):
    return client.post(
        "/mcp", headers=request_headers or headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def test_protocol_and_citations():
    service = FakeSearch()
    with TestClient(create_app(service)) as client:
        result = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        assert result.status_code == 200
        assert "tools" in result.json()["result"]["capabilities"]
        notification = client.post("/mcp", headers=headers(), json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert notification.status_code == 202
        tools = rpc(client, "tools/list", {}).json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["search_web"]
        assert tools[0]["inputSchema"]["properties"]["query"]["maxLength"] == 2000
        result = rpc(client, "tools/call", {"name": "search_web", "arguments": {"query": "Public query"}})
        data = result.json()["result"]
        assert not data["isError"]
        assert data["structuredContent"]["citations"][0]["url"] == "https://example.com"
        assert data["structuredContent"]["untrusted_web_content"] is True
    assert service.queries == ["Public query"]


@pytest.mark.parametrize("query", ["", "   ", "a" * 2001, 123])
def test_invalid_queries_do_not_call_foundry(query):
    service = FakeSearch()
    with TestClient(create_app(service)) as client:
        result = rpc(client, "tools/call", {"name": "search_web", "arguments": {"query": query}})
        assert result.json()["result"]["isError"]
    assert service.queries == []


def test_exact_query_limit():
    service = FakeSearch()
    with TestClient(create_app(service)) as client:
        data = rpc(client, "tools/call", {
            "name": "search_web", "arguments": {"query": "a" * 2000},
        }).json()
        assert not data["result"]["isError"]


def test_tool_failure_is_not_success():
    with TestClient(create_app(FakeSearch(fail=True))) as client:
        result = rpc(client, "tools/call", {"name": "search_web", "arguments": {"query": "query"}})
        assert result.json()["result"]["isError"]


@pytest.mark.parametrize("principal,status", [
    (None, 401),
    ("not base64", 401),
    (base64.b64encode(b"[]").decode(), 401),
    (base64.b64encode(b'{"auth_typ":"aad","claims":null}').decode(), 401),
])
def test_malformed_auth(principal, status):
    with TestClient(create_app(FakeSearch())) as client:
        h = {"Accept": "application/json, text/event-stream"}
        if principal:
            h["x-ms-client-principal"] = principal
        assert rpc(client, "tools/list", {}, h).status_code == status


def test_wrong_scope():
    with TestClient(create_app(FakeSearch())) as client:
        assert rpc(client, "tools/list", {}, headers("user.read")).status_code == 403
