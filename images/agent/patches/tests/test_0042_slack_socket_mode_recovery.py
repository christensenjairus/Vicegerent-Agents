#!/usr/bin/env python3
"""Behavioral regression test for bounded Slack Socket Mode recovery."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DRIVER = r'''
import asyncio
import importlib
import logging
import sys
import time


for name in list(sys.modules):
    if name.startswith("plugins.platforms.slack") or name.startswith("slack_sdk"):
        del sys.modules[name]

adapter_mod = importlib.import_module("plugins.platforms.slack.adapter")
sdk_mod = importlib.import_module("slack_sdk.socket_mode.aiohttp")
SlackAdapter = adapter_mod.SlackAdapter
SocketModeClient = sdk_mod.SocketModeClient


class ScaledAsyncio:
    """Delegate asyncio calls while shortening production wait_for timeouts."""

    def __init__(self, real, maximum):
        self._real = real
        self._maximum = maximum
        self.requested_timeouts = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def wait_for(self, awaitable, timeout):
        self.requested_timeouts.append(timeout)
        return await self._real.wait_for(awaitable, min(timeout, self._maximum))


class DisconnectedClient:
    last_ping_pong_time = None
    ping_interval = 1.0

    async def is_connected(self):
        return False


class HangingHandler:
    def __init__(self):
        self.client = DisconnectedClient()
        self.close_attempts = 0

    async def close_async(self):
        self.close_attempts += 1
        await asyncio.sleep(3600)


async def check_watchdog_recovers_repeatedly():
    interval = 0.01
    close_timeout = 0.02
    scaled = ScaledAsyncio(asyncio, close_timeout)
    adapter_mod.asyncio = scaled

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._running = True
    adapter._app = object()
    adapter._app_token = "x"
    adapter._proxy_url = None
    adapter._handler = HangingHandler()
    adapter._socket_mode_task = asyncio.create_task(asyncio.sleep(3600))
    adapter._socket_watchdog_task = None
    adapter._socket_reconnect_lock = asyncio.Lock()
    adapter._socket_watchdog_interval_s = interval
    adapter._socket_handler_started_monotonic = time.monotonic()
    adapter._socket_first_ping_grace_s = 1.0
    adapter._socket_ping_stale_factor = 4.0

    starts = 0

    def fake_start():
        nonlocal starts
        starts += 1
        adapter._handler = HangingHandler()
        adapter._socket_mode_task = asyncio.create_task(asyncio.sleep(3600))
        adapter._socket_handler_started_monotonic = time.monotonic()

    adapter._start_socket_mode_handler = fake_start
    watchdog = asyncio.create_task(adapter._socket_watchdog_loop())
    runtime = (interval + close_timeout) * 5
    await asyncio.sleep(runtime)
    still_polling = not watchdog.done()
    adapter._running = False
    watchdog.cancel()
    try:
        await watchdog
    except asyncio.CancelledError:
        pass
    if adapter._socket_mode_task is not None:
        adapter._socket_mode_task.cancel()

    assert starts >= 2 and still_polling, (
        "watchdog wedged on hanging close_async(): "
        f"reconnects={starts}, still_polling={still_polling}"
    )
    assert scaled.requested_timeouts
    assert set(scaled.requested_timeouts) == {10.0}, (
        "adapter teardown no longer uses the intended 10-second production bound: "
        f"{scaled.requested_timeouts!r}"
    )


class BlackHoleSession:
    closed = False

    def __init__(self):
        self.pings = 0

    async def ping(self, data=None):
        self.pings += 1
        await asyncio.sleep(3600)


async def check_sdk_ping_recovers():
    ping_interval = 0.01
    ping_timeout = 0.02
    scaled = ScaledAsyncio(asyncio, ping_timeout)
    sdk_mod.asyncio = scaled

    client = SocketModeClient.__new__(SocketModeClient)
    client.logger = logging.getLogger("patch0042")
    client.closed = False
    client.stale = False
    client.ping_interval = ping_interval
    client.trace_enabled = False
    client.last_ping_pong_time = time.time() - 1
    client.auto_reconnect_enabled = True
    client.default_auto_reconnect_enabled = True
    client.connect_operation_lock = asyncio.Lock()
    session = BlackHoleSession()
    client.current_session = session

    reconnects = 0

    async def fake_reconnect(force=False):
        nonlocal reconnects
        reconnects += 1

    client.connect_to_new_endpoint = fake_reconnect
    monitor = asyncio.create_task(client.monitor_current_session())
    runtime = (ping_interval + ping_timeout) * 5
    await asyncio.sleep(runtime)
    still_polling = not monitor.done()
    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass

    assert session.pings >= 2 and reconnects >= 1 and still_polling, (
        "SDK monitor wedged on hanging session.ping(): "
        f"attempts={session.pings}, reconnects={reconnects}, "
        f"still_polling={still_polling}"
    )
    assert scaled.requested_timeouts
    assert set(scaled.requested_timeouts) == {1.0}, (
        "SDK ping no longer derives its production timeout from ping_interval: "
        f"{scaled.requested_timeouts!r}"
    )


asyncio.run(check_watchdog_recovers_repeatedly())
asyncio.run(check_sdk_ping_recovers())
'''


def _find_slack_sdk() -> Path:
    configured = os.environ.get("HERMES_SLACK_SDK_ROOT")
    if configured:
        return Path(configured)
    spec = importlib.util.find_spec("slack_sdk")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("FAIL: cannot locate slack_sdk; set HERMES_SLACK_SDK_ROOT")
    return Path(next(iter(spec.submodule_search_locations)))


def _run_driver(root: Path, source_root: Path) -> subprocess.CompletedProcess[str]:
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
    slack_sdk_root = _find_slack_sdk()
    watched = {
        path: path.read_text(encoding="utf-8")
        for path in (
            source_root / "plugins" / "platforms" / "slack" / "adapter.py",
            slack_sdk_root / "socket_mode" / "aiohttp" / "__init__.py",
        )
    }
    patch = Path(__file__).resolve().parents[1] / "0042-slack-socket-mode-recovery.py"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        shutil.copytree(source_root / "agent", root / "agent")
        shutil.copytree(source_root / "plugins", root / "plugins")
        shutil.copytree(slack_sdk_root, root / "slack_sdk")

        if not args.pre_fix:
            env = {
                **os.environ,
                "PYTHONPATH": f"{root}:{source_root}",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for attempt in range(2):
                result = subprocess.run(
                    [sys.executable, str(patch)],
                    cwd="/",
                    env=env,
                    text=True,
                    capture_output=True,
                )
                if result.returncode:
                    raise SystemExit(
                        f"FAIL: patch attempt {attempt + 1} failed\n"
                        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                    )
            if "already bounded" not in result.stdout:
                raise SystemExit("FAIL: patch 0042 is not idempotent")

        result = _run_driver(root, source_root)
        if result.returncode:
            raise SystemExit(
                "FAIL: Slack Socket Mode recovery probe failed\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

    for path, before in watched.items():
        if path.read_text(encoding="utf-8") != before:
            raise SystemExit(f"FAIL: test mutated installed Hermes source: {path}")

    mode = "pre-fix" if args.pre_fix else "patched"
    print(f"PASS: patch 0042 {mode} behavioral probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
