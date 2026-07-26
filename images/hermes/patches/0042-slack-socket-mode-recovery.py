#!/usr/bin/env python3
"""Vicegerent patch: make Slack Socket Mode actually recover from a network hiccup.

Symptom
-------
After a transient network blip the gateway logged exactly one line::

    WARNING hermes_plugins.slack_platform.adapter: [Slack] Socket Mode unhealthy
        (transport disconnected); reconnecting

...and then went permanently deaf. The SDK churned reconnects for 20 minutes,
Slack delivered nothing, and only a pod restart recovered it.

Root cause: the watchdog wedges inside its own reconnect
--------------------------------------------------------
Timeline from the incident (agent.log, 2026-07-26)::

    02:04:13  adapter watchdog: transport disconnected; reconnecting  <-- fires ONCE
    02:04:13  new session s_..0304 established, "Bolt app is running!"
    02:20:02  SDK: session s_..0304 stale, disconnected for 939+ seconds
    02:20:03  new session s_..0019 established
    02:23:38  SDK: session s_..0019 stale, disconnected for 205+ seconds
    02:23:40  new session s_..0095 established
    02:24:42  SIGTERM (operator restarted the pod)

``_restart_socket_mode()`` takes ``_socket_reconnect_lock`` and then awaits
``_stop_socket_mode_handler()``, which awaits ``handler.close_async()``. That
writes a websocket CLOSE frame. On a black-holed TCP connection (peer gone, no
RST -- exactly a "hiccup") the write blocks until the kernel exhausts
``tcp_retries2`` retransmits; this host reports ``tcp_retries2=15`` ~= 805s+.

The watchdog loop ``await``s ``_restart_socket_mode()``, so the loop stops
polling, and the lock is never released, so no later reconnect can run either.
That is the observed signature exactly: one warning line, then total adapter
silence until SIGTERM, while the SDK's independent monitor churned reconnects.

Reproduced against the unpatched adapter by running the real
``_socket_watchdog_loop`` with a handler whose ``close_async()`` never returns:
over a window allowing ~15 polls, reconnect attempts = **0**, the loop was still
technically "alive", and ``_socket_reconnect_lock.locked()`` was **True**. With
the fix below the same scenario yields 3 reconnects and a loop that keeps polling.

Contributing defect: the SDK's own stale detection also wedges
-------------------------------------------------------------
``monitor_current_session()`` stamps ``last_ping_pong_time``, *then* awaits
``session.ping(...)``, and the staleness check sits *after* that await. The ping
write blocks on the same black-holed socket, so the stale branch is unreachable
and the SDK performs zero reconnects for the whole retransmit window.

The arithmetic confirms it: 02:20:02 minus 939s == 02:04:23 == exactly one
``ping_interval`` (10s) after the session was established. ``last_ping_pong_time``
was stamped on the monitor's first iteration and never advanced again. Probed
against the real monitor with a ping that never returns: ping entered once,
reconnects performed **0**.

Contributing defect: a post-reconnect window where nothing looks unhealthy
-------------------------------------------------------------------------
``is_connected()`` calls ``is_ping_pong_failing()``, which returns ``False`` when
``last_ping_pong_time is None``, and every ``connect()`` resets ``stale = False``.
So a socket that reconnects but never receives a single frame reports healthy
until the SDK's monitor stamps a time -- and if that monitor is itself wedged (see
above), the ``None`` persists and the adapter's only health probe says
"connected" indefinitely. Probed: with ``last_ping_pong_time=None``,
``is_connected()`` -> True; with a 900s-old stamp -> False.

Note this is a *bounded-window* bug, not the main event: once the monitor stamps
a value (~10s in) the frozen stamp does trip the stale test. Do not read it as
the explanation for the full 20 minutes.

Fix
---
a. **Bound the handler teardown** (adapter). ``asyncio.wait_for`` around
   ``handler.close_async()``, so a dead socket can no longer park the watchdog or
   strand ``_socket_reconnect_lock``. The task cancel that follows already
   guarantees the old reader stops, so abandoning the CLOSE frame is safe. This
   is the fix for the actual outage.

b. **Bound the ping write** (SDK, ``monitor_current_session``). ``asyncio.wait_for``
   around ``session.ping(...)`` so the SDK's own recovery path stays live. The
   existing ``except Exception`` already treats a failed ping as non-fatal and
   falls through to the staleness check, so a timeout now reaches the reconnect
   logic on the next iteration instead of being unreachable.

c. **Close the post-reconnect blind window** (adapter). Give each new connection a
   grace deadline: if the SDK has still not recorded a single ping/pong exchange
   ``VICEGERENT_SLACK_PONG_GRACE_S`` seconds after the handler started, treat the
   socket as unhealthy and reconnect. This uses the SDK's own
   ``last_ping_pong_time``, which advances every ``ping_interval`` (~10s)
   independently of chat traffic.

   Deliberately NOT keyed on inbound Slack messages: ``message_listeners`` are
   driven by ``enqueue_message()``, which ``receive_messages()`` reaches only for
   ``WSMsgType.TEXT`` -- websocket PING/PONG frames ``continue`` without
   enqueuing. A quiet DM therefore produces zero listener activity indefinitely,
   and real gateway.log gaps between user messages on one healthy session reached
   1250s. A "no traffic for N seconds" rule would have reconnected a perfectly
   good socket every few minutes.

Reconnect escalation: a plain reconnect reuses ``self.wss_uri``. If that endpoint
is itself the problem, retrying it forever gets nowhere -- so after
``_SOCKET_ESCALATE_AFTER`` consecutive unhealthy verdicts the patch clears
``wss_uri``, forcing ``connect()`` to fetch a fresh endpoint via
``apps.connections.open``.

Remove this patch once upstream slack_sdk bounds its ping write and Hermes bounds
its Socket Mode handler teardown.
"""

import asyncio
import importlib.util
import os
import sys

APPLIED_MARKER = "vicegerent-patch-0042"

# ---------------------------------------------------------------------------
# Anchors (verified to appear exactly once each against the live sources)
# ---------------------------------------------------------------------------

ANCHOR_SDK_PING = """                        try:
                            await session.ping(f"sdk-ping-pong:{t}".encode("utf-8"))
                        except Exception as e:"""

REPLACEMENT_SDK_PING = """                        try:
                            # vicegerent-patch-0042: bound the ping write. On a
                            # black-holed TCP connection this write blocks until the
                            # kernel exhausts tcp_retries2 (~800s+), and the staleness
                            # check below is unreachable for that whole window, so no
                            # reconnect happens. A timeout falls through to the
                            # existing handler, which treats a failed ping as
                            # non-fatal and lets the stale branch run.
                            await asyncio.wait_for(
                                session.ping(f"sdk-ping-pong:{t}".encode("utf-8")),
                                timeout=max(float(self.ping_interval), 1.0),
                            )
                        except Exception as e:"""

ANCHOR_INIT = """        self._socket_reconnect_lock = asyncio.Lock()
        self._socket_watchdog_interval_s = 15.0
"""

REPLACEMENT_INIT = """        self._socket_reconnect_lock = asyncio.Lock()
        self._socket_watchdog_interval_s = 15.0
        # vicegerent-patch-0042: is_connected() cannot distinguish "freshly
        # reconnected" from "reconnected and stone deaf" -- last_ping_pong_time is
        # None => is_ping_pong_failing() is False. Bound that window with a grace
        # deadline measured from when each handler started.
        self._socket_started_at: float = 0.0
        self._socket_unhealthy_streak: int = 0
        self._socket_pong_grace_s: float = _vicegerent_pong_grace()
        self._socket_close_timeout_s: float = _vicegerent_close_timeout()
        self._SOCKET_ESCALATE_AFTER: int = 3
"""

ANCHOR_STOP = """        if handler is not None:
            try:
                await handler.close_async()
            except Exception as e:  # pragma: no cover - defensive logging
"""

REPLACEMENT_STOP = """        if handler is not None:
            try:
                # vicegerent-patch-0042: THE outage fix. close_async() writes a
                # CLOSE frame, which blocks for the kernel retransmit window
                # (~800s) on a black-holed socket. This runs while
                # _socket_reconnect_lock is held and the watchdog loop awaits it,
                # so an unbounded wait parks the watchdog AND strands the lock --
                # one warning line, then permanent deafness. The task cancel below
                # already stops the old reader, so abandoning the frame is safe.
                await asyncio.wait_for(
                    handler.close_async(),
                    timeout=getattr(self, "_socket_close_timeout_s", 10.0),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Slack] Socket Mode handler close timed out after %.0fs; "
                    "abandoning the old connection",
                    getattr(self, "_socket_close_timeout_s", 10.0),
                )
            except Exception as e:  # pragma: no cover - defensive logging
"""

ANCHOR_WATCHDOG = """                connected = await self._socket_transport_connected()
                if connected is False:
                    await self._restart_socket_mode("transport disconnected")
"""

REPLACEMENT_WATCHDOG = """                connected = await self._socket_transport_connected()
                if connected is False:
                    self._socket_unhealthy_streak += 1
                    if self._socket_unhealthy_streak >= self._SOCKET_ESCALATE_AFTER:
                        # Plain reconnects reuse self.wss_uri. If that endpoint is
                        # the problem, force a fresh one from apps.connections.open.
                        self._socket_force_new_endpoint()
                        self._socket_unhealthy_streak = 0
                    await self._restart_socket_mode("transport disconnected")
                    continue

                # vicegerent-patch-0042: a socket can report connected purely
                # because the SDK has not recorded a ping/pong yet (None reads as
                # healthy). If no exchange lands within the grace window, the
                # connection is not coming up -- reconnect instead of trusting it.
                silent_for = self._socket_pong_silence()
                if silent_for is not None and silent_for > self._socket_pong_grace_s:
                    self._socket_unhealthy_streak += 1
                    if self._socket_unhealthy_streak >= self._SOCKET_ESCALATE_AFTER:
                        self._socket_force_new_endpoint()
                        self._socket_unhealthy_streak = 0
                    await self._restart_socket_mode(
                        f"no ping/pong exchange {int(silent_for)}s after connect "
                        f"(grace {int(self._socket_pong_grace_s)}s)"
                    )
                else:
                    self._socket_unhealthy_streak = 0
"""

# Helper methods + module-level config readers appended to the adapter.
ADAPTER_HELPERS = '''

# --- vicegerent-patch-0042: Socket Mode recovery helpers ---------------------


def _vicegerent_env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _vicegerent_pong_grace() -> float:
    """Seconds a new connection may go without any ping/pong before it's unhealthy.

    Must exceed the watchdog poll interval, or we'd reconnect on every tick.
    """
    return _vicegerent_env_float("VICEGERENT_SLACK_PONG_GRACE_S", 90.0, 30.0)


def _vicegerent_close_timeout() -> float:
    """Cap on handler.close_async() so a dead socket can't park the watchdog."""
    return _vicegerent_env_float("VICEGERENT_SLACK_CLOSE_TIMEOUT_S", 10.0, 1.0)


def _vicegerent_socket_pong_silence(self):
    """Seconds since a ping/pong was last recorded, or None if not measurable.

    Falls back to the handler's start time while the SDK has recorded nothing --
    that None is exactly the state is_connected() misreports as healthy.
    """
    if not self._running or self._socket_mode_task is None:
        return None
    client = getattr(self._handler, "client", None)
    if client is None:
        return None
    started = getattr(self, "_socket_started_at", 0.0)
    last = getattr(client, "last_ping_pong_time", None)
    reference = last if last else started
    if not reference:
        return None
    return time.time() - reference


def _vicegerent_socket_force_new_endpoint(self) -> None:
    """Drop the cached WSS URL so the next connect() fetches a fresh endpoint."""
    client = getattr(self._handler, "client", None)
    if client is None:
        return
    try:
        client.wss_uri = None
        logger.warning(
            "[Slack] Repeated unhealthy verdicts; forcing a fresh Socket Mode "
            "endpoint on the next reconnect"
        )
    except Exception:  # pragma: no cover - optional client API
        logger.debug("[Slack] Could not clear cached WSS URL", exc_info=True)


SlackAdapter._socket_pong_silence = _vicegerent_socket_pong_silence
SlackAdapter._socket_force_new_endpoint = _vicegerent_socket_force_new_endpoint

# Stamp the start time on every handler bring-up (initial connect AND every
# watchdog reconnect) so the grace window is measured per connection.
_vicegerent_orig_start = SlackAdapter._start_socket_mode_handler


def _vicegerent_start_socket_mode_handler(self) -> None:
    _vicegerent_orig_start(self)
    self._socket_started_at = time.time()
    self._socket_unhealthy_streak = 0


SlackAdapter._start_socket_mode_handler = _vicegerent_start_socket_mode_handler

# vicegerent-patch-0042: handler teardown and the SDK ping write are bounded, the
# watchdog reconnects a connection that never completes a ping/pong, and repeated
# unhealthy verdicts force a fresh WSS endpoint.
'''


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch 0042: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _locate(module: str, human: str) -> str:
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin:
        raise SystemExit(f"patch 0042: cannot locate {human}")
    return spec.origin


def _patch_sdk_ping() -> None:
    path = _locate("slack_sdk.socket_mode.aiohttp", "slack_sdk/socket_mode/aiohttp")

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch 0042: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_SDK_PING, path, "monitor_current_session() ping write")
    src = src.replace(ANCHOR_SDK_PING, REPLACEMENT_SDK_PING, 1)

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch 0042: bounded the Socket Mode ping write in {path}")


def _patch_adapter() -> None:
    path = _locate(
        "plugins.platforms.slack.adapter", "plugins/platforms/slack/adapter.py"
    )

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch 0042: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_INIT, path, "__init__ watchdog-state block")
    src = src.replace(ANCHOR_INIT, REPLACEMENT_INIT, 1)

    _count_or_raise(src, ANCHOR_STOP, path, "_stop_socket_mode_handler close_async")
    src = src.replace(ANCHOR_STOP, REPLACEMENT_STOP, 1)

    _count_or_raise(src, ANCHOR_WATCHDOG, path, "watchdog transport-probe branch")
    src = src.replace(ANCHOR_WATCHDOG, REPLACEMENT_WATCHDOG, 1)

    src += ADAPTER_HELPERS

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch 0042: bounded teardown + added pong-grace watchdog in {path}")


# ---------------------------------------------------------------------------
# Behavioral verification -- the defects, reproduced against the patched code.
# ---------------------------------------------------------------------------


def _verify() -> None:
    print("patch 0042: verifying...")

    # Locating the adapter via find_spec() imports its parent package, whose
    # __init__.py does `from .adapter import register` -- so the PRE-patch module
    # is already cached in sys.modules by the time we get here. Purge it (and the
    # patched SDK module) so the assertions below run against the patched files on
    # disk rather than a stale in-memory copy that would pass falsely.
    for name in [
        n
        for n in sys.modules
        if n.startswith("plugins.platforms.slack") or n.startswith("slack_sdk")
    ]:
        del sys.modules[name]

    import importlib
    import inspect
    import time as _time

    adapter_mod = importlib.import_module("plugins.platforms.slack.adapter")
    adapter_path = adapter_mod.__file__ or ""
    with open(adapter_path, "r", encoding="utf-8") as f:
        if APPLIED_MARKER not in f.read():
            raise SystemExit(
                "patch 0042: loaded adapter module is not the patched file "
                f"({adapter_path}) -- refusing to verify a stale import"
            )

    from slack_sdk.socket_mode.aiohttp import SocketModeClient

    if "wait_for" not in inspect.getsource(SocketModeClient.monitor_current_session):
        raise SystemExit(
            "patch 0042: loaded slack_sdk is not the patched file -- "
            "refusing to verify a stale import"
        )

    SlackAdapter = adapter_mod.SlackAdapter

    # -- 1. THE outage: the real watchdog loop must survive a handler whose
    #       close_async() never returns. Unpatched this yields 0 reconnects, a
    #       loop that stops polling, and a permanently held reconnect lock.
    class HangingClient:
        def __init__(self):
            self.wss_uri = "wss://fake/endpoint"
            self.last_ping_pong_time = None

        async def is_connected(self):
            return False

    class HangingHandler:
        def __init__(self):
            self.client = HangingClient()

        async def close_async(self):
            await asyncio.sleep(3600)

    async def check_watchdog_survives():
        a = SlackAdapter.__new__(SlackAdapter)
        a._running = True
        a._app = object()
        a._app_token = "x"
        a._proxy_url = None
        a._handler = HangingHandler()
        a._socket_reconnect_lock = asyncio.Lock()
        a._socket_watchdog_interval_s = 0.2
        a._socket_watchdog_task = None
        a._socket_started_at = _time.time()
        a._socket_unhealthy_streak = 0
        a._socket_pong_grace_s = 90.0
        a._socket_close_timeout_s = 1.0  # scaled down from 10s
        a._SOCKET_ESCALATE_AFTER = 3

        starts = {"n": 0}

        def fake_start():
            starts["n"] += 1
            a._handler = HangingHandler()
            a._socket_started_at = _time.time()

        a._start_socket_mode_handler = fake_start
        a._socket_mode_task = asyncio.create_task(asyncio.sleep(3600))

        loop_task = asyncio.create_task(a._socket_watchdog_loop())
        await asyncio.sleep(6.0)  # room for several bounded teardowns
        polling = not loop_task.done()
        loop_task.cancel()
        if a._socket_mode_task is not None:
            a._socket_mode_task.cancel()
        return starts["n"], polling

    reconnects, polling = asyncio.run(check_watchdog_survives())
    if reconnects < 2 or not polling:
        raise SystemExit(
            "patch 0042: watchdog still wedges on a dead socket "
            f"(reconnects={reconnects}, still_polling={polling}); "
            "expected repeated reconnects and a live loop"
        )
    print(
        f"  ok  watchdog survives a hung close_async() "
        f"(reconnects={reconnects}, still polling={polling})"
    )

    # -- 2. A ping that never returns must no longer wedge the SDK monitor.
    class BlackHoleSession:
        closed = False

        def __init__(self):
            self.pings = 0

        async def ping(self, data=None):
            self.pings += 1
            await asyncio.sleep(3600)

        async def close(self):
            await asyncio.sleep(3600)

    async def check_ping_bounded():
        import logging

        c = SocketModeClient.__new__(SocketModeClient)
        c.logger = logging.getLogger("patch0042")
        c.closed = False
        c.stale = False
        c.ping_interval = 1.0
        c.trace_enabled = False
        c.last_ping_pong_time = None
        c.auto_reconnect_enabled = True
        c.default_auto_reconnect_enabled = True
        c.connect_operation_lock = asyncio.Lock()
        session = BlackHoleSession()
        c.current_session = session

        reconnects = {"n": 0}

        async def fake_reconnect(force=False):
            reconnects["n"] += 1

        c.connect_to_new_endpoint = fake_reconnect

        monitor = asyncio.create_task(c.monitor_current_session())
        # is_ping_pong_failing() trips at ping_interval * 4, and each iteration
        # costs sleep(interval) + the bounded ping timeout.
        await asyncio.sleep(c.ping_interval * 12)
        monitor.cancel()
        return session.pings, reconnects["n"]

    pings, sdk_reconnects = asyncio.run(check_ping_bounded())
    if pings < 2 or sdk_reconnects < 1:
        raise SystemExit(
            "patch 0042: ping write still unbounded "
            f"(attempts={pings}, reconnects={sdk_reconnects})"
        )
    print(
        f"  ok  blocked ping no longer wedges the SDK monitor "
        f"(attempts={pings}, reconnects={sdk_reconnects})"
    )

    # -- 3. Config readers must reject junk and sub-floor values.
    for env, fn, cases in [
        (
            "VICEGERENT_SLACK_PONG_GRACE_S",
            adapter_mod._vicegerent_pong_grace,
            [("", 90.0), ("junk", 90.0), ("5", 90.0), ("240", 240.0)],
        ),
        (
            "VICEGERENT_SLACK_CLOSE_TIMEOUT_S",
            adapter_mod._vicegerent_close_timeout,
            [("", 10.0), ("junk", 10.0), ("0", 10.0), ("25", 25.0)],
        ),
    ]:
        for raw, want in cases:
            if raw:
                os.environ[env] = raw
            else:
                os.environ.pop(env, None)
            got = fn()
            if got != want:
                raise SystemExit(
                    f"patch 0042: {env}={raw!r} produced {got}, expected {want}"
                )
        os.environ.pop(env, None)
    print("  ok  both config readers handle junk / too-small / valid values")

    # -- 4. The pong-grace probe: a connection that never exchanges ping/pong is
    #       unhealthy, but a fresh one and an actively-ponging one are not. This is
    #       the case is_connected() misreports as healthy.
    class ProbeClient:
        def __init__(self, lppt):
            self.last_ping_pong_time = lppt
            self.wss_uri = "wss://fake"

    def probe(started_ago, lppt):
        a = SlackAdapter.__new__(SlackAdapter)
        a._running = True
        a._socket_mode_task = object()
        a._socket_pong_grace_s = 90.0
        a._socket_started_at = _time.time() - started_ago
        a._handler = type("H", (), {"client": ProbeClient(lppt)})()
        return a._socket_pong_silence()

    fresh = probe(5, None)
    never = probe(600, None)
    ponging = probe(600, _time.time() - 8)
    if fresh is None or fresh > 90:
        raise SystemExit(f"patch 0042: fresh connection judged silent ({fresh})")
    if never is None or never <= 90:
        raise SystemExit(f"patch 0042: never-ponged connection judged healthy ({never})")
    if ponging is None or ponging > 90:
        raise SystemExit(f"patch 0042: actively-ponging connection judged silent")

    stopped = SlackAdapter.__new__(SlackAdapter)
    stopped._running = False
    stopped._socket_mode_task = None
    if stopped._socket_pong_silence() is not None:
        raise SystemExit("patch 0042: silence should be None when not running")
    print(
        f"  ok  pong-grace probe: fresh={fresh:.0f}s ok, never-ponged={never:.0f}s "
        f"unhealthy, ponging={ponging:.0f}s ok, stopped=None"
    )

    # -- 5. An IDLE but healthy connection must NOT be reconnected. Real
    #       gateway.log gaps between user messages on one good session reached
    #       1250s, so a traffic-based rule would storm. Assert the pong signal is
    #       what's consulted, and that 1250s of chat silence is still healthy.
    idle_healthy = probe(1250, _time.time() - 7)
    if idle_healthy is None or idle_healthy > 90:
        raise SystemExit(
            "patch 0042: an idle-but-ponging connection was judged unhealthy "
            f"({idle_healthy}) -- this would reconnect-storm on quiet DMs"
        )
    silence_src = inspect.getsource(SlackAdapter._socket_pong_silence)
    if "last_ping_pong_time" not in silence_src:
        raise SystemExit("patch 0042: liveness probe ignores last_ping_pong_time")
    if "message_listeners" in silence_src:
        raise SystemExit(
            "patch 0042: liveness probe keyed on chat traffic -- PING/PONG frames "
            "never reach message_listeners, so quiet DMs would storm"
        )
    print(
        f"  ok  idle connection ({1250}s with no chat traffic) stays healthy "
        f"via ping/pong, not message traffic"
    )

    # -- 6. The watchdog must consume the verdict and not fall through.
    wd = inspect.getsource(SlackAdapter._socket_watchdog_loop)
    for needle in ("_socket_pong_silence", "_socket_pong_grace_s", "no ping/pong"):
        if needle not in wd:
            raise SystemExit(f"patch 0042: watchdog loop missing {needle!r}")
    if "continue" not in wd:
        raise SystemExit("patch 0042: watchdog transport branch must not fall through")
    print("  ok  watchdog loop consumes the pong-grace verdict")

    # -- 7. Escalation clears the cached endpoint, and start-up stamps the window.
    esc = SlackAdapter.__new__(SlackAdapter)
    esc._handler = type("H", (), {"client": ProbeClient(None)})()
    esc._socket_force_new_endpoint()
    if esc._handler.client.wss_uri is not None:
        raise SystemExit("patch 0042: escalation did not clear the cached WSS URL")
    if "_socket_started_at" not in inspect.getsource(
        SlackAdapter._start_socket_mode_handler
    ):
        raise SystemExit(
            "patch 0042: _start_socket_mode_handler does not stamp the grace window; "
            "reconnects would inherit a stale deadline"
        )
    print("  ok  escalation clears wss_uri; every bring-up restamps the grace window")


def main() -> int:
    _patch_sdk_ping()
    _patch_adapter()
    _verify()
    print("Patch 0042 applied and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
