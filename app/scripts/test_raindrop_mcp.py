"""
Test script: Raindrop.io MCP connection

Connects to the Raindrop.io MCP server via Streamable HTTP, initializes
the session, and lists all available tools.

Usage:
    uv run python scripts/test_raindrop_mcp.py

Requires:
    RAINDROP_IO_TEST_TOKEN — personal access token from https://app.raindrop.io/settings/integrations
"""

import os
import sys
from pathlib import Path

import anyio
from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

MCP_URL = "https://api.raindrop.io/rest/v2/ai/mcp"


def get_token() -> str:
    token = os.getenv("RAINDROP_IO_TEST_TOKEN")
    if not token:
        print("ERROR: RAINDROP_IO_TEST_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    return token


async def run():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Connecting to Raindrop.io MCP server: {MCP_URL}")
    print("-" * 60)

    async with streamablehttp_client(MCP_URL, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            print("Initializing MCP session...")
            init_result = await session.initialize()
            print(f"✓ Connected — server: {init_result.serverInfo.name} v{init_result.serverInfo.version}")
            print(f"  Protocol version: {init_result.protocolVersion}")
            print()

            print("Listing available tools...")
            tools_result = await session.list_tools()

            if not tools_result.tools:
                print("  (no tools returned)")
            else:
                print(f"  Found {len(tools_result.tools)} tool(s):\n")
                for tool in tools_result.tools:
                    print(f"  🔧 {tool.name}")
                    if tool.description:
                        print(f"     {tool.description}")
                    print()

    print("-" * 60)
    print("✓ Done.")


if __name__ == "__main__":
    anyio.run(run, backend="asyncio")
