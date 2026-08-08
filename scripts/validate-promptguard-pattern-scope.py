#!/usr/bin/env python3
"""Validate directional model prompt-guard scope and fixture hygiene."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "images/mcp-cerbos-shim/internal/server/secret-patterns.json"
EXPECTED_REQUEST_ONLY = {
    "http_bearer_token",
    "http_basic_auth",
    "us_ssn",
    "credit_card_visa",
    "credit_card_mastercard",
    "credit_card_amex",
    "credit_card_discover",
    "us_phone_number",
}


def fail(message: str) -> None:
    print(f"FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "-C", str(REPO), "ls-files", "-z"]
    )
    return [REPO / raw.decode("utf-8", "surrogateescape") for raw in output.split(b"\0") if raw]


def main() -> None:
    definitions = json.loads(REGISTRY.read_text(encoding="utf-8"))
    request_only = {
        item["name"]
        for item in definitions
        if item.get("modelResponse", True) is False
    }
    if request_only != EXPECTED_REQUEST_ONLY:
        fail(
            "request-only prompt-guard patterns changed: "
            f"got {sorted(request_only)}, want {sorted(EXPECTED_REQUEST_ONLY)}"
        )

    response_definitions = [
        item for item in definitions if item.get("modelResponse", True) is not False
    ]
    if len(definitions) != 41 or len(response_definitions) != 33:
        fail(
            f"unexpected prompt-guard counts: request={len(definitions)} "
            f"response={len(response_definitions)}"
        )

    compiled = {
        item["name"]: re.compile(item["regex"], re.ASCII)
        for item in definitions
    }
    response_patterns = [
        (item["name"], compiled[item["name"]]) for item in response_definitions
    ]
    hits: list[tuple[str, str]] = []
    scanned = 0
    for path in tracked_files():
        try:
            if path.stat().st_size > 400_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for name, pattern in response_patterns:
            if pattern.search(text):
                hits.append((str(path.relative_to(REPO)), name))
    if hits:
        rendered = ", ".join(f"{path}:{name}" for path, name in hits[:20])
        fail(
            "tracked source contains literal model-response-blocking fixtures; "
            f"construct them at runtime instead: {rendered}"
        )
    if not scanned:
        fail("tracked-file corpus was empty")

    print(
        "PASS - all 41 request patterns remain enabled; 33 high-confidence response "
        f"patterns reject output; {scanned} tracked files contain no blocking fixture"
    )


if __name__ == "__main__":
    main()
