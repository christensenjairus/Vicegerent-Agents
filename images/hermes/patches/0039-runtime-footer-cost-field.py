#!/usr/bin/env python3
"""Add turn/session cost to the v2026.8.3 runtime footer.

Upstream v2026.8.3 added native ``latency`` rendering and already measures the
whole turn with ``time.monotonic()``. Preserve that implementation instead of
carrying Vicegerent's older duplicate ``duration`` field. This patch only
snapshots the session cost around ``run_conversation()``, computes the turn
delta, threads both values through the two result dictionaries, and adds the
``cost`` footer field. It runs after patches 0028 and 0036.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "Vicegerent patch 0039 for v2026.8.3"


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"patch 0039: expected 1 {label} anchor, found {count}")
    return source.replace(old, new, 1)


def patch_runtime_footer() -> None:
    spec = importlib.util.find_spec("gateway.runtime_footer")
    if spec is None or not spec.origin:
        raise SystemExit("patch 0039: cannot locate gateway/runtime_footer.py")
    path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"patch 0039: runtime footer already patched in {path}")
        return

    source = replace_once(
        source,
        """    turn_seconds: Optional[float] = None,
    effort: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
""",
        """    turn_seconds: Optional[float] = None,
    effort: Optional[str] = None,
    turn_cost_usd: Optional[float] = None,
    session_cost_usd: Optional[float] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
""",
        "format signature",
    )
    source = replace_once(
        source,
        """        elif field == "effort":
            # Vicegerent patch 0028: not an upstream field -- see run.py's
            # agent.reasoning_config for where this value comes from.
            if effort:
                parts.append(str(effort))
        # Unknown field names are silently ignored.
""",
        """        elif field == "effort":
            # Vicegerent patch 0028: not an upstream field -- see run.py's
            # agent.reasoning_config for where this value comes from.
            if effort:
                parts.append(str(effort))
        elif field == "cost":
            if turn_cost_usd is not None and session_cost_usd is not None:
                parts.append(
                    f"${turn_cost_usd:.2f} turn · ${session_cost_usd:.2f} session"
                )
        # Unknown field names are silently ignored.
""",
        "cost field",
    )
    source = replace_once(
        source,
        """    turn_seconds: Optional[float] = None,
    effort: Optional[str] = None,
) -> str:
""",
        """    turn_seconds: Optional[float] = None,
    effort: Optional[str] = None,
    turn_cost_usd: Optional[float] = None,
    session_cost_usd: Optional[float] = None,
) -> str:
""",
        "build signature",
    )
    source = replace_once(
        source,
        """        turn_seconds=turn_seconds,
        effort=effort,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
""",
        """        turn_seconds=turn_seconds,
        effort=effort,
        turn_cost_usd=turn_cost_usd,
        session_cost_usd=session_cost_usd,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
""",
        "build call",
    )

    source += f"\n# {MARKER}: added the cost field; upstream latency remains native.\n"
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"patch 0039: runtime footer cost field added in {path}")


def patch_gateway_run() -> None:
    spec = importlib.util.find_spec("gateway.run")
    if spec is None or not spec.origin:
        raise SystemExit("patch 0039: cannot locate gateway/run.py")
    path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"patch 0039: gateway run already patched in {path}")
        return

    source = replace_once(
        source,
        """            if _persist_user_timestamp_override is not None:
                _conversation_kwargs["persist_user_timestamp"] = _persist_user_timestamp_override
            result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
""",
        """            if _persist_user_timestamp_override is not None:
                _conversation_kwargs["persist_user_timestamp"] = _persist_user_timestamp_override
            _session_cost_before = getattr(agent, "session_estimated_cost_usd", None)
            result = agent.run_conversation(_api_run_message, **_conversation_kwargs)
            _session_cost_after = getattr(agent, "session_estimated_cost_usd", None)
            _session_cost_available = (
                getattr(agent, "session_cost_status", "unknown") != "unknown"
                and _session_cost_before is not None
                and _session_cost_after is not None
            )
            _turn_cost_usd = (
                max(0.0, float(_session_cost_after) - float(_session_cost_before))
                if _session_cost_available
                else None
            )
            if not _session_cost_available:
                _session_cost_after = None
""",
        "run_conversation cost snapshot",
    )
    source = replace_once(
        source,
        """                "context_length": _context_length,
                # Vicegerent patch 0028: feeds runtime_footer.py's
""",
        """                "context_length": _context_length,
                "turn_cost_usd": _turn_cost_usd,
                "session_cost_usd": _session_cost_after,
                # Vicegerent patch 0028: feeds runtime_footer.py's
""",
        "early result",
    )
    source = replace_once(
        source,
        """            "context_length": _context_length,
            # Vicegerent patch 0028: feeds runtime_footer.py's "effort"
""",
        """            "context_length": _context_length,
            "turn_cost_usd": _turn_cost_usd,
            "session_cost_usd": _session_cost_after,
            # Vicegerent patch 0028: feeds runtime_footer.py's "effort"
""",
        "final result",
    )
    source = replace_once(
        source,
        """                        effort=agent_result.get("reasoning_effort") or None,
                        turn_seconds=_turn_seconds,
""",
        """                        effort=agent_result.get("reasoning_effort") or None,
                        turn_seconds=_turn_seconds,
                        turn_cost_usd=agent_result.get("turn_cost_usd"),
                        session_cost_usd=agent_result.get("session_cost_usd"),
""",
        "footer call",
    )

    source += f"\n# {MARKER}: threaded turn/session cost into the native-latency footer.\n"
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"patch 0039: turn/session cost threaded through {path}")


def main() -> int:
    patch_runtime_footer()
    patch_gateway_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
