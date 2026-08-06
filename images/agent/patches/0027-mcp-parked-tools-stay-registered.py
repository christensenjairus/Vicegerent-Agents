#!/usr/bin/env python3
"""Keep MCP tools visible while an upstream v2026.8.3 server is parked.

Upstream now tracks parked lifecycle state with ``_was_parked`` and clears it
only after a session proves healthy. Reuse that state instead of adding the old
``_parked`` slot, remove only the five park-time deregistration calls (shutdown
still deregisters), keep parked tools visible through ``_make_check_fn``, and
return a backoff error without waking the timed self-probe on every call.

The session-expiry classifier fix formerly carried here landed upstream: it now
walks exception groups and recognizes message-less AnyIO transport exceptions.
Fail loudly on every expected anchor and remain idempotent.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

MARKER = "Vicegerent patch 0027 for v2026.8.3"


def replace_exact(source: str, old: str, new: str, expected: int, label: str) -> str:
    count = source.count(old)
    if count != expected:
        raise SystemExit(
            f"patch 0027: expected {expected} {label} anchor(s), found {count}"
        )
    return source.replace(old, new)


def main() -> int:
    spec = importlib.util.find_spec("tools.mcp_tool")
    if spec is None or not spec.origin:
        raise SystemExit("patch 0027: cannot locate tools/mcp_tool.py")
    path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")

    if MARKER in source:
        print(f"patch 0027: already applied to {path}")
        return 0

    park_pattern = re.compile(
        r"(?P<indent> +)self\._was_parked = True\n"
        r"(?P=indent)self\._deregister_tools\(\)\n"
        r"(?P=indent)self\._reconnect_event\.clear\(\)"
    )
    source, park_count = park_pattern.subn(
        lambda match: (
            f"{match.group('indent')}self._was_parked = True\n"
            f"{match.group('indent')}# {MARKER}: retain schemas while the live task self-probes.\n"
            f"{match.group('indent')}self._reconnect_event.clear()"
        ),
        source,
    )
    if park_count != 5:
        raise SystemExit(
            f"patch 0027: expected 5 park-time deregistration anchors, found {park_count}"
        )

    source = replace_exact(
        source,
        """            if server is not None and (
                server.session is not None or server._is_recycled_stdio()
            ):
""",
        """            if server is not None and (
                server.session is not None
                or server._is_recycled_stdio()
                or server._was_parked
            ):
""",
        1,
        "check_fn visibility",
    )

    parked_result = (
        "        if server is not None and server._was_parked and server.session is None:\n"
        "            return tool_error(\n"
        "                f\"MCP server '{server_name}' is parked after repeated connection \"\n"
        "                \"failures. Its tools remain registered but are temporarily \"\n"
        "                \"unavailable; retry on a later turn after the timed self-probe.\"\n"
        "            )\n"
    )
    source = replace_exact(
        source,
        """        server = _get_connected_server_for_call(server_name)
        if not server:
""",
        """        server = _get_connected_server_for_call(server_name)
""" + parked_result + """        if not server:
""",
        1,
        "tools/call parked",
    )
    source = replace_exact(
        source,
        """        server = _get_connected_server_for_call(server_name)
        if not server or not server.session:
""",
        """        server = _get_connected_server_for_call(server_name)
""" + parked_result + """        if not server or not server.session:
""",
        4,
        "resource/prompt parked",
    )

    recovery_pattern = re.compile(
        r"(?P<block>            recovered = _handle_session_expired_and_retry\(\n"
        r"(?:.*\n){1,6}?"
        r"            if recovered is not None:\n"
        r"                return recovered\n)"
        r"(?P<log>            logger\.error\()"
    )

    def add_recovery_message(match: re.Match[str]) -> str:
        return (
            match.group("block")
            + "            if _is_session_expired_error(exc):\n"
            + "                logger.warning(\n"
            + "                    \"MCP %s call still unavailable after automatic transport recovery\",\n"
            + "                    server_name,\n"
            + "                )\n"
            + "                return tool_error(\n"
            + "                    f\"MCP server '{server_name}' transport recovery was already \"\n"
            + "                    \"attempted and the call still failed. Do not retry immediately; \"\n"
            + "                    \"back off and try on a later turn.\"\n"
            + "                )\n"
            + match.group("log")
        )

    source, recovery_count = recovery_pattern.subn(add_recovery_message, source)
    if recovery_count != 4:
        raise SystemExit(
            f"patch 0027: expected 4 resource/prompt recovery fallthrough anchors, found {recovery_count}"
        )

    main_recovery_anchor = """            if recovered is not None:
                return recovered

            # Per-backend circuit breaker scoping for vMCP optimizer
"""
    main_recovery_replacement = """            if recovered is not None:
                return recovered
            if _is_session_expired_error(exc):
                logger.warning(
                    "MCP %s call still unavailable after automatic transport recovery",
                    server_name,
                )
                return tool_error(
                    f"MCP server '{server_name}' transport recovery was already "
                    "attempted and the call still failed. Do not retry immediately; "
                    "back off and try on a later turn."
                )

            # Per-backend circuit breaker scoping for vMCP optimizer
"""
    source = replace_exact(
        source,
        main_recovery_anchor,
        main_recovery_replacement,
        1,
        "tools/call recovery fallthrough",
    )

    source = replace_exact(
        source,
        """        reconnect budget is exhausted, so a dead server never leaves phantom
        tool definitions bloating the prompt cache and producing "not
        connected" errors on every turn.
""",
        """        reconnect budget is exhausted in upstream. The Vicegerent parked-tools
        patch removes those park-time calls so schemas remain stable; shutdown
        remains the only caller that permanently clears registrations.
""",
        1,
        "deregister docstring",
    )

    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"patch 0027: parked MCP tools remain registered and callable in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
