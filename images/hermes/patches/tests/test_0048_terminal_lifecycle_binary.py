#!/usr/bin/env python3
"""Regression coverage for patch 0048's terminal lifecycle integration.

Run after patch application:

    HERMES_SOURCE_ROOT=/path/to/hermes python3 test_0048_terminal_lifecycle_binary.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

source_root = Path(os.environ.get("HERMES_SOURCE_ROOT", "/opt/hermes"))
# terminal_tool owns asynchronous process state through interpreter shutdown, so
# do not delete its isolated HOME before atexit handlers finish.
home = tempfile.mkdtemp(prefix="hermes-0048-test-")
os.environ["HERMES_HOME"] = home
os.environ["HOME"] = home
os.environ["_HERMES_GATEWAY"] = "1"
sys.path.insert(0, str(source_root))

marker_source = (source_root / "cron" / "lifecycle_guard.py").read_text()
assert "vicegerent-patch-0048" in marker_source, (
    f"patch 0048 is absent from HERMES_SOURCE_ROOT={source_root}"
)

import tools.terminal_tool as terminal_module  # pyright: ignore[reportMissingImports]  # noqa: E402
from cron.lifecycle_guard import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    contains_gateway_lifecycle_command_or_referenced_script,
)

result = json.loads(
    terminal_module.terminal_tool(
        f'{sys.executable} -c "print(12345)"',
        timeout=20,
    )
)
assert result.get("exit_code") == 0, result
assert result.get("output", "").strip() == "12345", result
assert "embedded null" not in json.dumps(result).lower(), result
print("PASS: an absolute Python binary executes instead of crashing the guard")

binary_remote_reads: list[str] = []
assert not contains_gateway_lifecycle_command_or_referenced_script(
    sys.executable,
    cwd=home,
    read_remote_script=lambda path: binary_remote_reads.append(path) or "unexpected",
)
assert binary_remote_reads == [], binary_remote_reads
print("PASS: a present local executable does not trigger the remote-read fallback")

protected = "hermes gateway " + "restart"
blocked = json.loads(terminal_module.terminal_tool(protected, timeout=5))
assert blocked.get("status") == "error", blocked
assert blocked.get("exit_code") == 1, blocked
assert "cannot restart or stop the gateway" in blocked.get("error", ""), blocked
print("PASS: direct gateway-lifecycle control remains blocked")

nul_script = "#!/bin/bash\n" + protected.replace("restart", "rest\x00art") + "\n"
local_script = Path(home) / "nul-wrapper.sh"
local_script.write_bytes(nul_script.encode())
assert contains_gateway_lifecycle_command_or_referenced_script(
    str(local_script), cwd=home
)
print("PASS: local intra-token NUL is deleted to mirror Bash and still blocked")

mz_script = Path(home) / "mz-wrapper.sh"
mz_script.write_text(f"MZ=1\n{protected}\n")
assert contains_gateway_lifecycle_command_or_referenced_script(
    str(mz_script), cwd=home
)
print("PASS: MZ-prefixed shell text is not misclassified as a PE executable")

remote_path = "/remote-only/nul-wrapper.sh"
remote_reads: list[str] = []


def read_remote(path: str) -> str:
    remote_reads.append(path)
    return nul_script


assert contains_gateway_lifecycle_command_or_referenced_script(
    remote_path, cwd=home, read_remote_script=read_remote
)
assert remote_reads == [remote_path], remote_reads
print("PASS: remote intra-token NUL is deleted to mirror Bash and still blocked")

remote_elf = "/remote-only/python"
assert not contains_gateway_lifecycle_command_or_referenced_script(
    remote_elf,
    cwd=home,
    read_remote_script=lambda _path: f"\x7fELF\x00binary-data\n{protected}\n",
)
print("PASS: corroborated remote ELF content is skipped without false blocking")
