#!/usr/bin/env python3
"""Bound Slack Socket Mode ping and teardown waits on v2026.8.3.

Hermes v2026.8.3 upstreamed the adapter-side watchdog, first-ping grace window,
transport task monitoring, and background-task cancellation that the older
Vicegerent patch carried. Two black-holed-socket waits remain unbounded:
``SocketModeClient.monitor_current_session()`` can hang in ``session.ping()``,
and ``SlackAdapter._stop_socket_mode_handler()`` can hang in
``handler.close_async()``. Bound only those waits and leave upstream's newer
recovery state machine intact.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "vicegerent-patch-0042-v2026.8.3"


def locate(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin:
        raise SystemExit(f"patch 0042: cannot locate {module}")
    return Path(spec.origin)


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"patch 0042: expected 1 {label} anchor, found {count}")
    return source.replace(old, new, 1)


def patch_sdk_ping() -> None:
    path = locate("slack_sdk.socket_mode.aiohttp")
    source = path.read_text(encoding="utf-8")
    if "vicegerent-patch-0042" in source:
        print(f"patch 0042: SDK ping already bounded in {path}")
        return
    source = replace_once(
        source,
        """                        try:
                            await session.ping(f"sdk-ping-pong:{t}".encode("utf-8"))
                        except Exception as e:""",
        """                        try:
                            # vicegerent-patch-0042: prevent a black-holed socket
                            # write from blocking stale-session recovery for the
                            # kernel TCP retransmit window.
                            await asyncio.wait_for(
                                session.ping(f"sdk-ping-pong:{t}".encode("utf-8")),
                                timeout=max(float(self.ping_interval), 1.0),
                            )
                        except Exception as e:""",
        "SDK ping",
    )
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"patch 0042: bounded Socket Mode ping in {path}")


def patch_adapter_close() -> None:
    path = locate("plugins.platforms.slack.adapter")
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"patch 0042: adapter close already bounded in {path}")
        return
    source = replace_once(
        source,
        """        if handler is not None:
            try:
                await handler.close_async()
            except Exception as e:  # pragma: no cover - defensive logging
""",
        """        if handler is not None:
            try:
                # vicegerent-patch-0042-v2026.8.3: upstream now cancels the
                # reader/monitor tasks before this point, so abandoning a CLOSE
                # frame that cannot be written is safe and must not wedge the
                # reconnect lock for the kernel retransmit window.
                await asyncio.wait_for(handler.close_async(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[Slack] Socket Mode handler close timed out after 10s; "
                    "abandoning the old connection"
                )
            except Exception as e:  # pragma: no cover - defensive logging
""",
        "adapter close_async",
    )
    source += f"\n# {MARKER}: bounded Socket Mode handler teardown.\n"
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    print(f"patch 0042: bounded Socket Mode teardown in {path}")


def main() -> int:
    patch_sdk_ping()
    patch_adapter_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
