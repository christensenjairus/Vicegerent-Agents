#!/usr/bin/env python3
"""Regression test for the Claude Code vMCP stdio bridge."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP

BRIDGE = Path(
    os.environ.get(
        "VMCP_BRIDGE_UNDER_TEST",
        Path(__file__).with_name("vmcp-stdio-bridge.py"),
    )
)


def append_event(path: Path, event: str, kind: str, index: int) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": event,
                    "kind": kind,
                    "index": index,
                    "time_ns": time.monotonic_ns(),
                }
            )
            + "\n"
        )


def run_fake_server(port: int, log: Path) -> None:
    server = FastMCP(
        "fake-vmcp",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
    )

    @server.tool(name="find_tool")
    async def find_tool(tool_description: str) -> str:
        index = int(tool_description)
        append_event(log, "start", "find", index)
        await anyio.sleep(0.12)
        append_event(log, "end", "find", index)
        return json.dumps(
            {
                "tools": [
                    {
                        "name": "fake_read",
                        "description": "Read a fake value.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"index": {"type": "integer"}},
                            "required": ["index"],
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
        )

    @server.tool(name="call_tool")
    async def call_tool(
        tool_name: str,
        parameters: dict[str, Any],
    ) -> str:
        index = parameters["index"]
        append_event(log, "start", "backend", index)
        await anyio.sleep(0.12)
        append_event(log, "end", "backend", index)
        return json.dumps({"tool_name": tool_name, "index": index})

    server.run(transport="streamable-http")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_ready(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, stderr = process.communicate()
            raise RuntimeError(f"fake vMCP exited before readiness: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("fake vMCP did not become ready")


def max_overlap(events: list[dict[str, Any]], kind: str) -> int:
    relevant = [event for event in events if event["kind"] == kind]
    relevant.sort(
        key=lambda event: (
            event["time_ns"],
            0 if event["event"] == "start" else 1,
        )
    )
    active = 0
    peak = 0
    for event in relevant:
        active += 1 if event["event"] == "start" else -1
        peak = max(peak, active)
    return peak


async def exercise(url: str, trace: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(BRIDGE), url],
        env={
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "VMCP_STDIO_BRIDGE_TRACE": str(trace),
        },
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {"find_tool", "call_tool", "batch_call_tool"}
            assert tools["find_tool"].annotations is not None
            assert tools["find_tool"].annotations.readOnlyHint is True
            assert tools["batch_call_tool"].annotations is not None
            assert tools["batch_call_tool"].annotations.readOnlyHint is False
            assert tools["batch_call_tool"].inputSchema["properties"]["calls"]["maxItems"] == 8

            find_results: list[types.CallToolResult | None] = [None] * 8

            async def find_one(index: int) -> None:
                find_results[index] = await session.call_tool(
                    "find_tool",
                    {"tool_description": str(index)},
                )

            async with anyio.create_task_group() as tasks:
                for index in range(8):
                    tasks.start_soon(find_one, index)
            assert all(result is not None and not result.isError for result in find_results)

            calls = [
                {"tool_name": "fake_read", "parameters": {"index": index}}
                for index in range(8)
            ]
            batch = await session.call_tool("batch_call_tool", {"calls": calls})
            assert not batch.isError
            assert batch.structuredContent is not None
            results = batch.structuredContent["results"]
            assert [result["tool_name"] for result in results] == ["fake_read"] * 8
            assert not any(result["result"].get("isError") for result in results)
            returned = [
                json.loads(result["result"]["content"][0]["text"])["index"]
                for result in results
            ]
            assert returned == list(range(8))

            rejected = await session.call_tool(
                "batch_call_tool",
                {"calls": [*calls, calls[0]]},
            )
            assert rejected.isError



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake-server", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    if args.fake_server:
        if args.port is None or args.log is None:
            parser.error("--fake-server requires --port and --log")
        run_fake_server(args.port, args.log)
        return

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        server_log = work / "server.jsonl"
        bridge_trace = work / "bridge.jsonl"
        port = free_port()
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--fake-server",
                "--port",
                str(port),
                "--log",
                str(server_log),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost"},
        )
        try:
            wait_ready(process, port)
            anyio.run(exercise, f"http://127.0.0.1:{port}/mcp", bridge_trace)
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

        events = [
            json.loads(line)
            for line in server_log.read_text(encoding="utf-8").splitlines()
        ]
        assert max_overlap(events, "find") == 8
        assert max_overlap(events, "backend") == 8
        assert sum(event["kind"] == "backend" for event in events) == 16

    print("PASS: Claude vMCP bridge executes bounded eight-call backend batches")


if __name__ == "__main__":
    main()
