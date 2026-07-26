#!/usr/bin/env python3
"""Assert the skills scripts are baked into the image and the chart calls them there.

The scripts moved from a mounted ConfigMap to /usr/local/bin in the image. That
swap has two halves that can drift apart silently: the Dockerfile must COPY them,
and the chart must invoke the baked-in path rather than a /reload mount that no
longer exists. A missing half fails at runtime, not at render time.

    python3 test_skills_scripts_baked.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
DOCKERFILE = REPO / "images" / "hermes" / "Dockerfile"
SRC_DIR = REPO / "images" / "hermes" / "skills-scripts"
CHART = REPO / "charts" / "agent" / "templates"

SCRIPTS = ["sync-shared-skills.sh", "snapshot-skills.sh"]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok ' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)


dockerfile = DOCKERFILE.read_text(encoding="utf-8")
chart_text = "\n".join(
    p.read_text(encoding="utf-8") for p in CHART.rglob("*") if p.is_file()
)

for name in SCRIPTS:
    src = SRC_DIR / name
    check(f"source exists: {name}", src.is_file())
    if src.is_file():
        rc = subprocess.run(["bash", "-n", str(src)], capture_output=True, text=True)
        check(f"valid bash: {name}", rc.returncode == 0, rc.stderr.strip()[:80])
    check(f"Dockerfile COPYs {name}", f"skills-scripts/{name}" in dockerfile)
    check(f"Dockerfile chmod +x {name}", f"/usr/local/bin/{name}" in dockerfile)

# The ConfigMap is gone: nothing may still reference the old mount path.
check(
    "no chart reference to the removed /reload/shared-skills mount",
    "/reload/shared-skills" not in chart_text,
)
check(
    "no chart reference to a shared-skills ConfigMap",
    not re.search(r"name:\s*\{\{[^}]*\}\}-shared-skills", chart_text),
)
check(
    "shared-skills.yaml template deleted",
    not (CHART / "shared-skills.yaml").exists(),
)

# Both scripts must actually be invoked, or baking them in is dead weight, and
# every call site must use the absolute path -- a bare name depends on the
# invoking context's PATH, which the hook runner does not guarantee.
for name in SCRIPTS:
    check(f"chart invokes {name}", name in chart_text)
    bare = [
        ln.strip()
        for ln in chart_text.split("\n")
        if name in ln and f"/usr/local/bin/{name}" not in ln
    ]
    check(f"every {name} call site is absolute", not bare, str(bare))

# The post_tool_call hooks must use an absolute baked-in path: the hook runner
# does not necessarily inherit the container's PATH.
helpers = (CHART / "_helpers.tpl").read_text(encoding="utf-8")
hook_cmds = re.findall(r"command:\s*(\S+)", helpers)
for name in SCRIPTS:
    check(
        f"{name} hook uses an absolute /usr/local/bin path",
        f"/usr/local/bin/{name}" in hook_cmds,
        str(hook_cmds),
    )

print("\n" + ("all checks passed" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
