"""Interactive end-to-end acceptance test from a private-network-connected PC."""

import argparse
import asyncio
import os
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from azure.identity import InteractiveBrowserCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def verify(
    url: str,
    auth_headers: dict[str, str],
    query: str,
    verify_wrong_function_key: bool = False,
) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.post(
            url,
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
        )
        if response.status_code != 401:
            raise RuntimeError(f"Expected anonymous rejection (401), got {response.status_code}.")
        if verify_wrong_function_key:
            response = await http.post(
                url,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "x-functions-key": "deliberately-invalid-key",
                },
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            )
            if response.status_code != 401:
                raise RuntimeError(
                    f"Expected wrong-key rejection (401), got {response.status_code}."
                )
    async with streamablehttp_client(
        url,
        headers=auth_headers,
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
            checks = (
                "anonymous/wrong-key rejection"
                if verify_wrong_function_key
                else "anonymous rejection"
            )
            print(f"PASS: {checks}, initialize, tools/list, tools/call, citations.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--client-id")
    parser.add_argument("--api-client-id")
    parser.add_argument(
        "--function-key-env",
        help="Read a Functions host key from this environment variable instead of using Entra OAuth.",
    )
    parser.add_argument(
        "--query",
        default="Microsoft Foundry の Web Search の公式ドキュメントを検索してください。",
    )
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a trusted HTTPS MCP URL from the infrastructure deployment output.")
    if args.function_key_env:
        function_key = os.environ.get(args.function_key_env)
        if not function_key:
            raise ValueError(f"Environment variable {args.function_key_env!r} is not set.")
        asyncio.run(
            verify(
                args.url,
                {"x-functions-key": function_key},
                args.query,
                verify_wrong_function_key=True,
            )
        )
        return
    missing = [
        name
        for name, value in (
            ("--tenant-id", args.tenant_id),
            ("--client-id", args.client_id),
            ("--api-client-id", args.api_client_id),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"{', '.join(missing)} required unless --function-key-env is specified."
        )
    with InteractiveBrowserCredential(
        tenant_id=args.tenant_id,
        client_id=args.client_id,
        redirect_uri="http://localhost:8765",
    ) as credential:
        token = credential.get_token(f"api://{args.api_client_id}/search")
        asyncio.run(
            verify(
                args.url,
                {"Authorization": f"Bearer {token.token}"},
                args.query,
            )
        )


if __name__ == "__main__":
    main()
