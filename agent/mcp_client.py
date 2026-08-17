"""Synchronous MCP call helper (Streamable HTTP).

The graph's nodes are sync functions (PostgresSaver is sync) while the MCP client is async, so each
call spins up its own event loop and connection (stateless HTTP; the local overhead is negligible).
This module **holds no server URL of its own**: the lookup URL is defaulted inside lookup_call, and
the action URL is allowed to appear only in graph.execute_action (topology invariant 3).
"""

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class McpToolError(RuntimeError):
    """The tool returned is_error (this includes business guardrail blocks, BLOCKED[...])."""


def _unwrap(exc: BaseException) -> McpToolError | None:
    """anyio's TaskGroup wraps tool errors in a (Base)ExceptionGroup; dig out the McpToolError.

    Without the unwrap, a guardrail block (BLOCKED[business_guardrail]) arrives as an ExceptionGroup,
    sails straight past execute_action's except clause, and crashes the whole graph instead of being
    recorded as blocked. See failures.md.
    """
    if isinstance(exc, McpToolError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _unwrap(sub)
            if found is not None:
                return found
    return None


def mcp_call(url: str, tool: str, args: dict) -> dict | list:
    async def _run():
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, args)
                text = res.content[0].text if res.content else ""
                if res.is_error:
                    raise McpToolError(text)
                return json.loads(text)

    try:
        return asyncio.run(_run())
    except BaseExceptionGroup as eg:
        found = _unwrap(eg)
        if found is not None:
            raise found from None
        raise


def lookup_call(tool: str, args: dict) -> dict | list:
    url = os.environ.get("MCP_LOOKUP_URL", "http://localhost:8101/mcp")
    return mcp_call(url, tool, args)
