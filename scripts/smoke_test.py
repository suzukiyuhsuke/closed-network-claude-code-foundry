"""Interactive end-to-end acceptance test from a private-network-connected PC."""

import argparse
import asyncio
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from azure.identity import InteractiveBrowserCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def verify(url: str, token: str, query: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(
            url,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
        )
        if response.status_code != 401:
            raise RuntimeError(f"Expected anonymous rejection (401), got {response.status_code}.")
    async with streamablehttp_client(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timedelta(seconds=200),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            if [tool.name for tool in tools.tools] != ["search_web"]:
                raise RuntimeError("Unexpected MCP tool catalog.")
            result = await session.call_tool("search_web", {"query": query})
            if result.isError:
                raise RuntimeError(f"MCP tool failed: {result.content}")
            data = result.structuredContent
            if not data or not data.get("answer") or not data.get("citations"):
                raise RuntimeError("Missing answer or citations.")
            print(data["answer"])
            for citation in data["citations"]:
                print(f'{citation["title"]}: {citation["url"]}')
            print("PASS: anonymous rejection, initialize, tools/list, tools/call, citations.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--api-client-id", required=True)
    parser.add_argument("--query", default="Microsoft Foundry の Web Search の公式ドキュメントを検索してください。")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a trusted HTTPS MCP URL from the infrastructure deployment output.")
    with InteractiveBrowserCredential(
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        redirect_uri="http://localhost:8765",
    ) as credential:
        token = credential.get_token(f"api://{args.api_client_id}/search")
        asyncio.run(verify(args.url, token.token, args.query))


if __name__ == "__main__":
    main()
