#!/usr/bin/env python3
"""Regression test for the trusted webhook proxy patch 0053."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("patched_webhook_0053", path)
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: could not load patched webhook module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def exercise(module) -> None:
    config = module.PlatformConfig(
        enabled=True,
        extra={
            "host": "0.0.0.0",
            "port": 8644,
            "routes": {
                "pagerduty-incidents": {
                    "trusted_proxy": True,
                    "events": ["incident.triggered"],
                    "prompt": "Incident {event.id}",
                }
            },
        },
    )
    adapter = module.WebhookAdapter(config)

    class Request:
        method = "POST"
        headers = {}
        match_info = {"route_name": "pagerduty-incidents"}
        content_length = 76

        async def read(self):
            return json.dumps(
                {
                    "event": {
                        "id": "evt-123",
                        "event_type": "incident.triggered",
                    }
                }
            ).encode()

    accepted_events = []

    async def accept_event(event):
        accepted_events.append(event)

    adapter.handle_message = accept_event
    response = await adapter._handle_webhook(Request())
    await asyncio.sleep(0)
    if response.status != 202 or len(accepted_events) != 1:
        raise SystemExit(
            f"FAIL: trusted secretless request status={response.status} events={len(accepted_events)}"
        )
    event = accepted_events[0]
    if event.message_id != "evt-123" or event.text != "Incident evt-123":
        raise SystemExit(
            f"FAIL: nested PagerDuty metadata was not preserved: {event.message_id!r} {event.text!r}"
        )

    bad_config = module.PlatformConfig(
        enabled=True,
        extra={
            "host": "0.0.0.0",
            "port": 0,
            "routes": {
                "bad": {
                    "trusted_proxy": True,
                    "secret": "must-not-enter-agent",  # pragma: allowlist secret
                }
            },
        },
    )
    bad_adapter = module.WebhookAdapter(bad_config)
    try:
        await bad_adapter.connect()
    except ValueError as exc:
        if "cannot combine trusted_proxy with a secret" not in str(exc):
            raise
    else:
        raise SystemExit("FAIL: trusted_proxy accepted signing material in the agent")


def main() -> int:
    source = Path(
        os.environ.get("HERMES_WEBHOOK_SOURCE", "/opt/hermes/gateway/platforms/webhook.py")
    )
    patch = Path(__file__).resolve().parents[1] / "0053-trusted-webhook-proxy.py"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "webhook.py"
        shutil.copy2(source, target)
        env = {**os.environ, "HERMES_WEBHOOK_PATH": str(target)}

        first = subprocess.run(
            [sys.executable, str(patch)], env=env, text=True, capture_output=True
        )
        if first.returncode != 0:
            raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
        if "trusted webhook proxy routes enabled" not in first.stdout:
            raise SystemExit("FAIL: first application did not transform pristine source")

        second = subprocess.run(
            [sys.executable, str(patch)], env=env, text=True, capture_output=True
        )
        if second.returncode != 0 or "already applied" not in second.stdout:
            raise SystemExit("FAIL: patch is not idempotent")

        module = load_module(target)
        asyncio.run(exercise(module))

    print("PASS: trusted listener routes need no agent-side signing secret")
    return 0


if __name__ == "__main__":
    sys.exit(main())
