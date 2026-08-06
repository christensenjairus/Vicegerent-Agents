#!/opt/hermes/.venv/bin/python
"""Expose vMCP to Claude Code with bounded concurrent backend batches."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

META_TOOLS = {"call_tool", "find_tool"}
BATCH_TOOL_NAME = "batch_call_tool"


def trace(path: Path | None, event: str, call_id: int, name: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        record = {
            "event": event,
            "id": call_id,
            "name": name,
            "time_ns": time.monotonic_ns(),
        }
        stream.write(json.dumps(record) + "\n")


def batch_tool() -> types.Tool:
    return types.Tool(
        name=BATCH_TOOL_NAME,
        description=(
            "Execute up to eight independent vMCP backend tool calls concurrently. "
            "Use this instead of multiple call_tool invocations when the calls do "
            "not depend on one another. Results are returned in input order; each "
            "result retains its own isError status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string", "minLength": 1},
                            "parameters": {"type": "object"},
                        },
                        "required": ["tool_name", "parameters"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["calls"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )


async def run(url: str) -> None:
    trace_file = os.environ.get("VMCP_STDIO_BRIDGE_TRACE")
    trace_path = Path(trace_file) if trace_file else None
    call_ids = itertools.count(1)
    timeout = httpx.Timeout(90, read=300)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
    ) as http_client:
        async with streamable_http_client(
            url,
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream, _):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=90),
            ) as remote:
                await remote.initialize()
                remote_tools = await remote.list_tools()
                base_tools = {
                    tool.name: tool
                    for tool in remote_tools.tools
                    if tool.name in META_TOOLS
                }
                if set(base_tools) != META_TOOLS:
                    missing = sorted(META_TOOLS - set(base_tools))
                    raise RuntimeError(f"vMCP optimizer tools missing: {missing}")
                base_tools["find_tool"] = base_tools["find_tool"].model_copy(
                    update={
                        "annotations": types.ToolAnnotations(
                            readOnlyHint=True,
                            destructiveHint=False,
                            idempotentHint=True,
                            openWorldHint=False,
                        )
                    }
                )

                server = Server("vmcp-stdio-bridge", version="1")

                @server.list_tools()
                async def list_tools() -> list[types.Tool]:
                    return [
                        base_tools["find_tool"],
                        base_tools["call_tool"],
                        batch_tool(),
                    ]

                async def invoke(
                    tool_name: str,
                    parameters: dict[str, Any],
                ) -> types.CallToolResult:
                    call_id = next(call_ids)
                    trace(trace_path, "start", call_id, tool_name)
                    try:
                        return await remote.call_tool(
                            "call_tool",
                            {"tool_name": tool_name, "parameters": parameters},
                        )
                    finally:
                        trace(trace_path, "end", call_id, tool_name)

                @server.call_tool()
                async def call_tool(
                    name: str,
                    arguments: dict[str, Any],
                ) -> types.CallToolResult:
                    if name in META_TOOLS:
                        call_id = next(call_ids)
                        trace(trace_path, "start", call_id, name)
                        try:
                            return await remote.call_tool(name, arguments)
                        finally:
                            trace(trace_path, "end", call_id, name)

                    if name != BATCH_TOOL_NAME:
                        return types.CallToolResult(
                            content=[
                                types.TextContent(
                                    type="text",
                                    text=f"Unknown vMCP bridge tool: {name}",
                                )
                            ],
                            isError=True,
                        )

                    calls = arguments["calls"]
                    results: list[dict[str, Any] | None] = [None] * len(calls)

                    async def run_one(
                        index: int,
                        call: dict[str, Any],
                    ) -> None:
                        try:
                            result = await invoke(
                                call["tool_name"],
                                call["parameters"],
                            )
                        except Exception as error:
                            result = types.CallToolResult(
                                content=[
                                    types.TextContent(
                                        type="text",
                                        text=str(error),
                                    )
                                ],
                                isError=True,
                            )
                        results[index] = {
                            "tool_name": call["tool_name"],
                            "result": result.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_none=True,
                            ),
                        }

                    async with anyio.create_task_group() as tasks:
                        for index, call in enumerate(calls):
                            tasks.start_soon(run_one, index, call)

                    payload = {"results": results}
                    return types.CallToolResult(
                        content=[
                            types.TextContent(
                                type="text",
                                text=json.dumps(payload),
                            )
                        ],
                        structuredContent=payload,
                    )

                async with stdio_server() as (local_read, local_write):
                    await server.run(
                        local_read,
                        local_write,
                        server.create_initialization_options(),
                    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    anyio.run(run, args.url)


if __name__ == "__main__":
    main()
