#!/usr/bin/env python3
"""Vicegerent patch: make opted-in deferred MCP calls fully concurrent.

Hermes v0.20 recognizes ``supports_parallel_tool_calls`` only when the batch
planner sees a registered MCP tool name directly. Progressive Tool Search makes
the model emit ``tool_call`` bridge calls instead, and the planner classifies the
bridge as a sequential barrier before the concurrent executor unwraps it.

The MCP client also takes one exclusive ``_rpc_lock`` around every user-visible
operation. That lock serializes calls to the same opted-in server, contradicting
the setting's documented same-server semantics. Replace it with a fair
shared/exclusive gate: parallel-safe single-request operations share access, while
discovery, refresh, and paginated listings retain exclusive access.

Fail loud on upstream drift and remain idempotent. Remove once upstream plans
deferred MCP calls by their underlying name and gives opted-in operations shared
per-server RPC access.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


DISPATCH_MARKER = "Vicegerent patch 0051: classify deferred MCP bridges"
DISPATCH_ANCHOR = f'''        if not isinstance(function_args, dict):
            logging.debug(
                "Non-dict args for %s (%s) {chr(8212)} treating as sequential barrier",
                tool_name,
                type(function_args).__name__,
            )
            _add_sequential(tool_call)
            continue

        if tool_name in _PATH_SCOPED_TOOLS:
'''
DISPATCH_REPLACEMENT = f'''        if not isinstance(function_args, dict):
            logging.debug(
                "Non-dict args for %s (%s) - treating as sequential barrier",
                tool_name,
                type(function_args).__name__,
            )
            _add_sequential(tool_call)
            continue

        # {DISPATCH_MARKER} before the executor unwraps them.
        try:
            from tools import tool_search as _tool_search
        except Exception:
            _tool_search = None
        if (
            _tool_search is not None
            and tool_name == _tool_search.TOOL_CALL_NAME
        ):
            try:
                underlying_name, underlying_args, bridge_error = (
                    _tool_search.resolve_underlying_call(function_args)
                )
            except Exception:
                underlying_name, underlying_args, bridge_error = None, {{}}, "resolve failed"
            if (
                bridge_error
                or not underlying_name
                or not _is_mcp_tool_parallel_safe(underlying_name)
            ):
                _add_sequential(tool_call)
                continue
            tool_name = underlying_name
            function_args = underlying_args

        if tool_name in _PATH_SCOPED_TOOLS:
'''

MCP_MARKER = "Vicegerent patch 0051: shared/exclusive MCP RPC gate"
MCP_CLASS_ANCHOR = '''class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task.
'''
MCP_CLASS_REPLACEMENT = f'''class _MCPRPCSharedScope:
    """Shared side of ``_MCPRPCGate`` for parallel-safe user operations."""

    def __init__(self, gate: "_MCPRPCGate"):
        self._gate = gate

    async def __aenter__(self):
        await self._gate._acquire_shared()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self._gate._release_shared()
        return False


class _MCPRPCGate:
    """{MCP_MARKER}.

    Parallel-safe tool operations take shared access. Discovery, refresh, and
    unflagged operations use the gate itself as an exclusive async context.
    Waiting writers block new readers so a busy server cannot starve refresh.
    """

    def __init__(self):
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    def locked(self) -> bool:
        return self._writer or self._readers > 0 or self._writers_waiting > 0

    def shared(self) -> _MCPRPCSharedScope:
        return _MCPRPCSharedScope(self)

    async def _acquire_shared(self) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._writers_waiting == 0
            )
            self._readers += 1

    async def _release_shared(self) -> None:
        async with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    async def __aenter__(self):
        async with self._condition:
            self._writers_waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer and self._readers == 0
                )
                self._writer = True
            finally:
                self._writers_waiting -= 1
                self._condition.notify_all()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        async with self._condition:
            self._writer = False
            self._condition.notify_all()
        return False


class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task.
'''

MCP_LOCK_ANCHOR = "        self._rpc_lock = asyncio.Lock()\n"
MCP_LOCK_REPLACEMENT = "        self._rpc_lock = _MCPRPCGate()\n"

MCP_HELPER_ANCHOR = '''def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
'''
MCP_HELPER_REPLACEMENT = '''def _mcp_rpc_scope_for_call(server_name: str, server: Any):
    """Return shared RPC access only when concurrency cannot misroute consent."""
    with _lock:
        parallel_safe = server_name in _parallel_safe_servers
    if parallel_safe and getattr(server, "_elicitation", None) is None:
        return server._rpc_lock.shared()
    return server._rpc_lock


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
'''

MCP_TOOL_CALL_LOCK_ANCHOR = '''            async with server._rpc_lock:
                # Snapshot the agent's context so an elicitation callback
'''
MCP_TOOL_CALL_LOCK_REPLACEMENT = '''            async with _mcp_rpc_scope_for_call(server_name, server):
                # Snapshot the agent's context so an elicitation callback
'''
MCP_READ_RESOURCE_LOCK_ANCHOR = '''            async with server._rpc_lock:
                result = await server.session.read_resource(uri)
'''
MCP_READ_RESOURCE_LOCK_REPLACEMENT = '''            async with _mcp_rpc_scope_for_call(server_name, server):
                result = await server.session.read_resource(uri)
'''
MCP_GET_PROMPT_LOCK_ANCHOR = '''            async with server._rpc_lock:
                result = await server.session.get_prompt(name, arguments=arguments)
'''
MCP_GET_PROMPT_LOCK_REPLACEMENT = '''            async with _mcp_rpc_scope_for_call(server_name, server):
                result = await server.session.get_prompt(name, arguments=arguments)
'''


def _patch_dispatch(source: str, path: Path) -> str:
    count = source.count(DISPATCH_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch 0051: expected exactly 1 deferred-planner anchor in {path}, "
            f"found {count} (upstream drifted - re-verify)"
        )
    patched = source.replace(DISPATCH_ANCHOR, DISPATCH_REPLACEMENT, 1)
    compile(patched, str(path), "exec")
    return patched


def _patch_mcp(source: str, path: Path) -> str:
    anchors = (
        ("RPC gate class", MCP_CLASS_ANCHOR, 1),
        ("RPC lock initialization", MCP_LOCK_ANCHOR, 1),
        ("parallel scope helper", MCP_HELPER_ANCHOR, 1),
        ("tool-call RPC lock", MCP_TOOL_CALL_LOCK_ANCHOR, 1),
        ("read-resource RPC lock", MCP_READ_RESOURCE_LOCK_ANCHOR, 1),
        ("get-prompt RPC lock", MCP_GET_PROMPT_LOCK_ANCHOR, 1),
        ("lifecycle lock checks", "self._rpc_lock.locked()", 2),
    )
    for label, anchor, expected in anchors:
        count = source.count(anchor)
        if count != expected:
            raise SystemExit(
                f"patch 0051: expected {expected} {label} anchor(s) in {path}, "
                f"found {count} (upstream drifted - re-verify)"
            )
    if source.count("_rpc_lock") != 12:
        raise SystemExit(
            f"patch 0051: expected 12 total _rpc_lock references in {path}, "
            f"found {source.count('_rpc_lock')} (upstream changed the lock API - re-verify)"
        )

    patched = source.replace(MCP_CLASS_ANCHOR, MCP_CLASS_REPLACEMENT, 1)
    patched = patched.replace(MCP_LOCK_ANCHOR, MCP_LOCK_REPLACEMENT, 1)
    patched = patched.replace(MCP_HELPER_ANCHOR, MCP_HELPER_REPLACEMENT, 1)
    patched = patched.replace(
        MCP_TOOL_CALL_LOCK_ANCHOR, MCP_TOOL_CALL_LOCK_REPLACEMENT, 1
    )
    patched = patched.replace(
        MCP_READ_RESOURCE_LOCK_ANCHOR, MCP_READ_RESOURCE_LOCK_REPLACEMENT, 1
    )
    patched = patched.replace(
        MCP_GET_PROMPT_LOCK_ANCHOR, MCP_GET_PROMPT_LOCK_REPLACEMENT, 1
    )
    compile(patched, str(path), "exec")
    return patched


def main() -> int:
    root = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
    dispatch_path = root / "agent" / "tool_dispatch_helpers.py"
    mcp_path = root / "tools" / "mcp_tool.py"

    dispatch_source = dispatch_path.read_text(encoding="utf-8")
    mcp_source = mcp_path.read_text(encoding="utf-8")
    dispatch_applied = DISPATCH_MARKER in dispatch_source
    mcp_applied = MCP_MARKER in mcp_source
    if dispatch_applied and mcp_applied:
        print("patch: already applied to Hermes MCP dispatch - no-op")
        return 0
    if dispatch_applied != mcp_applied:
        raise SystemExit(
            "patch 0051: partially applied state detected - restore both source "
            "files before retrying"
        )

    patched_dispatch = _patch_dispatch(dispatch_source, dispatch_path)
    patched_mcp = _patch_mcp(mcp_source, mcp_path)
    dispatch_path.write_text(patched_dispatch, encoding="utf-8")
    mcp_path.write_text(patched_mcp, encoding="utf-8")

    print(
        "patch: deferred parallel-safe MCP calls now use shared per-server RPC access"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
