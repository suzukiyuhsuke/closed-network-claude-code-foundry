import copy

import pytest
from openai.types.responses import Response

from search import SearchFailure, Settings, extract_result


def response_payload():
    return {
        "id": "resp_test", "created_at": 0, "object": "response",
        "model": "test-model", "parallel_tool_calls": True,
        "tool_choice": "required", "tools": [], "status": "completed",
        "output": [
            {"id": "search_1", "type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "test"}},
            {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "Test answer", "annotations": [
                 {"type": "url_citation", "url": "https://example.com", "title": "Example",
                  "start_index": 0, "end_index": 11},
             ]}]},
        ],
    }


def test_extract_result():
    result = extract_result(Response.model_validate(response_payload()))
    assert result.answer == "Test answer"
    assert result.citations[0].end_index == 11


def test_multiple_messages_preserve_citation_offsets():
    payload = response_payload()
    payload["output"].append(copy.deepcopy(payload["output"][1]))
    result = extract_result(Response.model_validate(payload))
    assert result.answer == "Test answer\n\nTest answer"
    assert result.citations[1].start_index == 13
    assert result.citations[1].end_index == len(result.answer)


@pytest.mark.parametrize("failure", ["incomplete", "no_search", "no_citations", "bad_url", "bad_offset"])
def test_failed_or_ungrounded_response(failure):
    payload = response_payload()
    if failure == "incomplete":
        payload["status"] = "incomplete"
    elif failure == "no_search":
        payload["output"] = payload["output"][1:]
    elif failure == "no_citations":
        payload["output"][1]["content"][0]["annotations"] = []
    elif failure == "bad_url":
        payload["output"][1]["content"][0]["annotations"][0]["url"] = "javascript:alert(1)"
    else:
        payload["output"][1]["content"][0]["annotations"][0]["end_index"] = 1000
    with pytest.raises(SearchFailure):
        extract_result(Response.model_validate(payload))


def set_env(monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://sample.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "search")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "1")
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-identity")
    monkeypatch.delenv("LOCAL_DEVELOPMENT", raising=False)


def test_production_settings(monkeypatch):
    set_env(monkeypatch)
    assert Settings.from_env().local is False


def test_local_bypass_rejected_in_azure(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("LOCAL_DEVELOPMENT", "true")
    monkeypatch.setenv("WEBSITE_HOSTNAME", "app.azurewebsites.net")
    with pytest.raises(ValueError, match="never"):
        Settings.from_env()


@pytest.mark.parametrize("endpoint", [
    "http://sample.services.ai.azure.com/api/projects/test",
    "https://example.com/api/projects/test",
    "https://sample.services.ai.azure.com/api/projects/test?token=secret",
    "https://sample.services.ai.azure.com/api/projects/",
    "https://sample.services.ai.azure.com/anthropic",
])
def test_invalid_endpoints(monkeypatch, endpoint):
    set_env(monkeypatch)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    with pytest.raises(ValueError):
        Settings.from_env()
