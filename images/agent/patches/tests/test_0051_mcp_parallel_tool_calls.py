#!/usr/bin/env python3
"""Regression test for patch 0051 (parallel deferred MCP calls)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DRIVER = r'''
import asyncio
import inspect
import json
from types import SimpleNamespace

from agent import tool_dispatch_helpers as dispatch, tool_executor
from tools import mcp_tool, tool_search

VMCP_TOOL = "mcp__vmcp__find_tool"
AGENTBURN_TOOL = "mcp__agentburn__burn_card"

# Keep this test independent of the process-global catalog assembled by AIAgent.
tool_search.is_deferrable_tool_name = lambda name: name in {
    VMCP_TOOL,
    AGENTBURN_TOOL,
}
dispatch._is_mcp_tool_parallel_safe = lambda name: name == VMCP_TOOL


def call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


assert tool_executor._MAX_TOOL_WORKERS == 8, (
    "Hermes must retain eight global parallel tool workers"
)

bridged_vmcp = [
    call(f"vmcp-{index}", "tool_call", {"name": VMCP_TOOL, "arguments": {}})
    for index in range(tool_executor._MAX_TOOL_WORKERS)
]
segments = dispatch._plan_tool_batch_segments(bridged_vmcp)
assert [kind for kind, _ in segments] == ["parallel"], (
    "eight model-emitted tool_call bridges for a parallel-safe MCP server must "
    f"reach the concurrent executor, got {segments!r}"
)
assert len(segments[0][1]) == tool_executor._MAX_TOOL_WORKERS

mixed_bridge = [
    call("vmcp", "tool_call", {"name": VMCP_TOOL, "arguments": {}}),
    call("agentburn", "tool_call", {"name": AGENTBURN_TOOL, "arguments": {}}),
]
assert [kind for kind, _ in dispatch._plan_tool_batch_segments(mixed_bridge)] == [
    "sequential"
], "an unflagged deferred MCP tool must remain a sequential barrier"

malformed_bridge = [
    call("bad-one", "tool_call", {"name": VMCP_TOOL, "arguments": []}),
    call("bad-two", "tool_call", {"name": VMCP_TOOL, "arguments": []}),
]
assert [kind for kind, _ in dispatch._plan_tool_batch_segments(malformed_bridge)] == [
    "sequential"
], "malformed bridge arguments must remain a sequential barrier"


async def measured_overlap(parallel, elicitation=None, worker_count=2):
    gate = mcp_tool._MCPRPCGate()
    server = SimpleNamespace(_rpc_lock=gate, _elicitation=elicitation)
    with mcp_tool._lock:
        if parallel:
            mcp_tool._parallel_safe_servers.add("vmcp")
        else:
            mcp_tool._parallel_safe_servers.discard("vmcp")

    active = 0
    maximum = 0

    async def worker():
        nonlocal active, maximum
        async with mcp_tool._mcp_rpc_scope_for_call("vmcp", server):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.04)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return maximum


assert asyncio.run(measured_overlap(parallel=False)) == 1, (
    "servers without supports_parallel_tool_calls must stay serialized"
)
assert asyncio.run(measured_overlap(parallel=True, worker_count=8)) == 8, (
    "parallel-safe calls to the same MCP server must use all eight worker slots"
)
assert asyncio.run(measured_overlap(parallel=True, elicitation=object())) == 1, (
    "elicitation-capable servers must stay serialized so approval context is not lost"
)


async def verify_refresh_exclusion():
    gate = mcp_tool._MCPRPCGate()
    order = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def first_reader():
        async with gate.shared():
            order.append("first-start")
            first_entered.set()
            await release_first.wait()
            order.append("first-end")

    async def writer():
        await first_entered.wait()
        async with gate:
            order.append("refresh-start")
            await asyncio.sleep(0)
            order.append("refresh-end")

    async def late_reader():
        async with gate.shared():
            order.append("late-start")
            order.append("late-end")

    first_task = asyncio.create_task(first_reader())
    await first_entered.wait()
    writer_task = asyncio.create_task(writer())
    while gate._writers_waiting == 0:
        await asyncio.sleep(0)
    assert gate.locked(), "a queued exclusive RPC must keep lifecycle recycling blocked"
    late_task = asyncio.create_task(late_reader())
    release_first.set()
    await asyncio.gather(first_task, writer_task, late_task)
    assert order.index("first-end") < order.index("refresh-start"), order
    assert order.index("refresh-end") < order.index("late-start"), order


asyncio.run(verify_refresh_exclusion())


async def verify_waiter_cancellation():
    gate = mcp_tool._MCPRPCGate()
    release_reader = asyncio.Event()
    reader_entered = asyncio.Event()

    async def holding_reader():
        async with gate.shared():
            reader_entered.set()
            await release_reader.wait()

    reader = asyncio.create_task(holding_reader())
    await reader_entered.wait()
    waiting_writer = asyncio.create_task(gate.__aenter__())
    while gate._writers_waiting == 0:
        await asyncio.sleep(0)
    waiting_writer.cancel()
    try:
        await waiting_writer
    except asyncio.CancelledError:
        pass
    release_reader.set()
    await reader
    async with asyncio.timeout(1):
        async with gate:
            pass

    async with gate:
        waiting_reader = asyncio.create_task(gate._acquire_shared())
        await asyncio.sleep(0)
        waiting_reader.cancel()
        try:
            await waiting_reader
        except asyncio.CancelledError:
            pass
    async with asyncio.timeout(1):
        async with gate.shared():
            pass


asyncio.run(verify_waiter_cancellation())

source = inspect.getsource(mcp_tool)
assert "self._rpc_lock = _MCPRPCGate()" in source
assert source.count(
    "async with _mcp_rpc_scope_for_call(server_name, server):"
) == 3, "single-RPC user operations must use the parallel-aware RPC scope"
assert source.count("async with server._rpc_lock:") == 2, (
    "paginated resource and prompt listings must retain exclusive RPC access"
)
'''


def run_driver(root: Path, source_root: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": f"{root}:{source_root}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", DRIVER],
        cwd="/",
        env=env,
        text=True,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre-fix", action="store_true")
    args = parser.parse_args()

    source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
    installed = {
        path: path.read_text(encoding="utf-8")
        for path in (
            source_root / "agent" / "tool_dispatch_helpers.py",
            source_root / "tools" / "mcp_tool.py",
        )
    }
    patch = Path(__file__).resolve().parents[1] / "0051-mcp-parallel-tool-calls.py"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        shutil.copytree(source_root / "agent", root / "agent")
        shutil.copytree(source_root / "tools", root / "tools")

        if not args.pre_fix:
            env = {**os.environ, "HERMES_ROOT": str(root)}
            first = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if first.returncode:
                raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
            second = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if second.returncode or "already applied" not in second.stdout:
                raise SystemExit("FAIL: patch is not idempotent")

        result = run_driver(root, source_root)
        if result.returncode:
            raise SystemExit(
                f"FAIL: parallel MCP regression probe failed\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

    for path, before in installed.items():
        if path.read_text(encoding="utf-8") != before:
            raise SystemExit(f"FAIL: test mutated installed Hermes source: {path}")

    print("PASS: deferred parallel-safe MCP calls use all eight global worker slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
