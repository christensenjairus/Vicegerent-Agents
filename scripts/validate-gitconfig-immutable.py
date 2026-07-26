#!/usr/bin/env python3
"""Assert the agent Sandbox owns ~/.gitconfig instead of seeding it imperatively.

The protected-branch guard's global-scope rung depends on manifest facts, not on
the shell scripts in images/hermes/git-guard/: the ConfigMap must carry
core.hooksPath, the mount must be readOnly, and nothing may write ~/.gitconfig at
runtime. Mode bits are deliberately NOT the assertion -- git replaces a config
file via lockfile+rename (verified: the inode changes), so only the read-only bind
mount denies the write. Run by scripts/validate.sh.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TRUSTED_HOOKS = "/opt/vicegerent/git-hooks"
GITCONFIG_PATH = "/opt/data/.gitconfig"


def die(msg: str) -> None:
    print(f"FAIL - {msg}", file=sys.stderr)
    sys.exit(1)


def render() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        defaults, machine = Path(tmp) / "d.yaml", Path(tmp) / "m.yaml"
        for src, dst in ((REPO / "values.defaults.yaml", defaults),
                         (REPO / "values.example.yaml", machine)):
            out = subprocess.run(["yq", ".agents[0]", str(src)],
                                 capture_output=True, text=True, check=True)
            dst.write_text(out.stdout)
        out = subprocess.run(
            ["helm", "template", str(REPO / "charts/agent"),
             "-f", str(defaults), "-f", str(machine)],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            die(f"helm template failed: {out.stderr.strip()[:400]}")
        return [d for d in yaml.safe_load_all(out.stdout) if d]


def main() -> None:
    docs = render()

    cms = [d for d in docs if d.get("kind") == "ConfigMap"
           and d["metadata"]["name"].endswith("-gitconfig")]
    if len(cms) != 1:
        die(f"expected exactly one -gitconfig ConfigMap, found {len(cms)}")
    body = cms[0].get("data", {}).get(".gitconfig")
    if not body:
        die("-gitconfig ConfigMap has no .gitconfig key")

    # Parse it with real git rather than string matching, so a malformed section
    # header or a tab/space slip fails here instead of silently at pod start.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".gitconfig"
        cfg.write_text(body)
        env = {"GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1",
               "HOME": tmp, "PATH": "/usr/bin:/bin"}
        got = {}
        for key in ("core.hooksPath", "user.name", "user.email"):
            out = subprocess.run(["/usr/bin/git", "config", "--get", key],
                                 capture_output=True, text=True, env=env, cwd=tmp)
            got[key] = out.stdout.strip()
    if got["core.hooksPath"] != TRUSTED_HOOKS:
        die(f"ConfigMap core.hooksPath is {got['core.hooksPath']!r}, want {TRUSTED_HOOKS!r}")
    for key in ("user.name", "user.email"):
        if not got[key]:
            die(f"ConfigMap does not set {key}; git identity would be lost")

    sandboxes = [d for d in docs if d.get("kind") == "Sandbox"]
    if not sandboxes:
        die("no Sandbox rendered")
    for sb in sandboxes:
        spec = sb["spec"]["podTemplate"]["spec"]
        containers = spec.get("containers", [])
        inits = spec.get("initContainers", [])

        agent_mounts = [m for c in containers for m in c.get("volumeMounts", [])
                        if m.get("mountPath") == GITCONFIG_PATH]
        if not agent_mounts:
            die(f"no container mounts {GITCONFIG_PATH}; the global rung stays writable")
        for m in agent_mounts:
            if m.get("subPath") != ".gitconfig":
                die(f"{GITCONFIG_PATH} mount must use subPath .gitconfig, got {m.get('subPath')!r}")
            if not m.get("readOnly"):
                die(f"{GITCONFIG_PATH} mount is not readOnly; the agent could replace it")

        vol_names = {m["name"] for m in agent_mounts}
        volumes = {v["name"]: v for v in spec.get("volumes", [])}
        for name in vol_names:
            v = volumes.get(name)
            if not v:
                die(f"volumeMount {name!r} has no matching volume")
                return
            if "configMap" not in v:
                die(f"volume {name!r} must be a configMap, got {sorted(v)}")

        # The imperative seed is what made the global rung writable in the first place.
        # Match executable lines only -- a comment mentioning the old approach is fine.
        for c in inits + containers:
            for arg in c.get("args", []):
                for line in arg.splitlines():
                    code = line.split("#", 1)[0]
                    if "git config --global" in code:
                        die(f"container {c['name']!r} still runs `git config --global` "
                            f"({line.strip()!r}); identity must come from the ConfigMap mount")

    print(f"OK - agent ~/.gitconfig is a readOnly ConfigMap mount pinning "
          f"core.hooksPath={TRUSTED_HOOKS}; no imperative git config --global remains")


if __name__ == "__main__":
    main()
