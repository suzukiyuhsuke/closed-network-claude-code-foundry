import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, WebSearchTool
from azure.core.credentials import AccessToken

from search import FoundrySearch, SearchFailure, Settings
from test_search import response_payload


class FakeCredential:
    async def get_token(self, *scopes, **kwargs):
        assert scopes == ("https://ai.azure.com/.default",)
        return AccessToken("fake-token-for-local-test", 4102444800)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_prompt_agent_tool_shape():
    definition = PromptAgentDefinition(model="search-model", tools=[WebSearchTool()])
    assert definition.as_dict()["tools"] == [{"type": "web_search"}]


@pytest.mark.parametrize("status", [200, 403, 429, 500])
async def test_real_sdk_request_and_error_contract(status):
    requests = []

    def handler(request):
        requests.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "PRIVATE UPSTREAM DETAIL"}})
        return httpx.Response(200, json=response_payload())

    original_client = AIProjectClient.get_openai_client
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        def get_client(project, **kwargs):
            return original_client(project, http_client=http, **kwargs)

        settings = Settings(
            endpoint="https://sample.services.ai.azure.com/api/projects/test",
            agent_name="shared-web-search", agent_version="7",
            local=False, identity_client_id="test-identity",
        )
        with patch("search.ManagedIdentityCredential", return_value=FakeCredential()), patch.object(
            AIProjectClient, "get_openai_client", get_client
        ):
            service = FoundrySearch(settings)
            if status == 200:
                result = await service.search("Public query")
                assert result.answer == "Test answer"
            else:
                with pytest.raises(SearchFailure) as error:
                    await service.search("Public query")
                assert "PRIVATE" not in str(error.value)
    assert len(requests) == 1
    assert requests[0].url.path == "/api/projects/test/openai/v1/responses"
    payload = json.loads(requests[0].content)
    assert payload["agent_reference"] == {
        "type": "agent_reference", "name": "shared-web-search", "version": "7",
    }
    assert payload["input"] == "Public query"
    assert payload["tool_choice"] == "required"
    assert payload["store"] is False
    assert "conversation" not in payload and "previous_response_id" not in payload


async def test_total_deadline_includes_authentication(monkeypatch):
    original_timeout = asyncio.timeout
    deadlines = []

    def short_timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(0.01)

    class SlowCredential(FakeCredential):
        async def get_token(self, *scopes, **kwargs):
            await asyncio.sleep(10)
            return await super().get_token(*scopes, **kwargs)

    settings = Settings(
        endpoint="https://sample.services.ai.azure.com/api/projects/test",
        agent_name="shared-web-search", agent_version="1",
        local=False, identity_client_id="test-identity",
    )
    monkeypatch.setattr("search.asyncio.timeout", short_timeout)
    with patch("search.ManagedIdentityCredential", return_value=SlowCredential()):
        with pytest.raises(SearchFailure, match="timed out"):
            await FoundrySearch(settings).search("Public query")
    assert deadlines == [180]
