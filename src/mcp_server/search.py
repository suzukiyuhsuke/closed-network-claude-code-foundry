import asyncio
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from openai import APIError
from openai.types.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SearchFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    endpoint: str
    agent_name: str
    agent_version: str
    local: bool
    identity_client_id: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".services.ai.azure.com")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/api/projects/")
            or len(parsed.path.split("/")) != 4
            or not parsed.path.split("/")[-1]
        ):
            raise ValueError("Use the Azure public cloud Foundry project endpoint.")
        name = os.environ["FOUNDRY_AGENT_NAME"].strip()
        version = os.environ["FOUNDRY_AGENT_VERSION"].strip()
        if not name or not version.isdecimal() or int(version) < 1:
            raise ValueError("An agent name and explicit positive version are required.")
        local = os.environ.get("LOCAL_DEVELOPMENT", "").lower() == "true"
        if local and (os.environ.get("WEBSITE_HOSTNAME") or os.environ.get("WEBSITE_INSTANCE_ID")):
            raise ValueError("LOCAL_DEVELOPMENT must never be enabled in Azure.")
        identity_id = os.environ.get("AZURE_CLIENT_ID")
        if not local and not identity_id:
            raise ValueError("AZURE_CLIENT_ID must identify the Functions managed identity.")
        return cls(endpoint, name, version, local, identity_id)


class Citation(BaseModel):
    url: str
    title: str
    start_index: int
    end_index: int


class SearchResult(BaseModel):
    answer: str
    citations: list[Citation]
    response_id: str
    untrusted_web_content: bool = True


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000, strict=True)


def extract_result(response: Response) -> SearchResult:
    if response.status != "completed":
        raise SearchFailure("Foundry response did not complete.")
    searched = any(
        item.type == "web_search_call" and item.status == "completed"
        for item in response.output
    )
    if not searched:
        raise SearchFailure("Foundry did not complete a web search.")
    parts: list[str] = []
    citations: list[Citation] = []
    offset = 0
    for item in response.output:
        if item.type != "message":
            continue
        for content in item.content:
            if content.type != "output_text":
                continue
            if parts:
                offset += 2
            parts.append(content.text)
            for annotation in content.annotations:
                if annotation.type == "url_citation":
                    parsed = urlsplit(annotation.url)
                    if parsed.scheme not in ("http", "https") or not parsed.hostname:
                        raise SearchFailure("Foundry returned an invalid citation URL.")
                    if not 0 <= annotation.start_index <= annotation.end_index <= len(content.text):
                        raise SearchFailure("Foundry returned invalid citation offsets.")
                    citations.append(
                        Citation(
                            url=annotation.url,
                            title=annotation.title,
                            start_index=offset + annotation.start_index,
                            end_index=offset + annotation.end_index,
                        )
                    )
            offset += len(content.text)
    answer = "\n\n".join(parts)
    if not answer.strip() or not citations:
        raise SearchFailure("No grounded answer with citations was returned; refine the query.")
    return SearchResult(answer=answer, citations=citations, response_id=response.id)


class FoundrySearch:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def search(self, query: str) -> SearchResult:
        query = SearchInput(query=query).query.strip()
        if not query:
            raise ValueError("Query must not be whitespace.")
        try:
            # Bound authentication and inference together below the HTTP frontend's limit.
            async with asyncio.timeout(180):
                credential = (
                    AzureCliCredential()
                    if self.settings.local
                    else ManagedIdentityCredential(client_id=self.settings.identity_client_id)
                )
                async with credential, AIProjectClient(
                    endpoint=self.settings.endpoint, credential=credential
                ) as project:
                    async with project.get_openai_client(
                        timeout=170, max_retries=0
                    ) as client:
                        response = await client.responses.create(
                            input=query,
                            tool_choice="required",
                            max_output_tokens=3000,
                            store=False,
                            extra_body={
                                "agent_reference": {
                                    "type": "agent_reference",
                                    "name": self.settings.agent_name,
                                    "version": self.settings.agent_version,
                                }
                            },
                        )
                        return extract_result(response)
        except TimeoutError:
            logger.warning("search_failed category=timeout")
            raise SearchFailure("Search timed out. Retry with a narrower query.") from None
        except (APIError, AzureError) as error:
            # SDK exception text can contain request content; never log it.
            logger.warning("search_failed category=%s", type(error).__name__)
            raise SearchFailure("Foundry request failed. Check service logs, RBAC and connectivity.") from None
