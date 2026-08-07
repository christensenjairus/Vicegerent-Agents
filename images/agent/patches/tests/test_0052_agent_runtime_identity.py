#!/usr/bin/env python3
"""Regression test for the neutral runtime-account patch 0052."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DOCKER_FILES = (
    "cont-init.d/015-supervise-perms",
    "cont-init.d/02-reconcile-profiles",
    "hermes-exec-shim.sh",
    "main-wrapper.sh",
    "s6-rc.d/dashboard/run",
    "stage2-hook.sh",
)


def main() -> int:
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "--verify-applied"):
        raise SystemExit("usage: test_0052_agent_runtime_identity.py [--verify-applied]")
    verify_applied = len(sys.argv) == 2
    source = Path(os.environ.get("HERMES_DOCKER_SOURCE", "/opt/hermes/docker"))
    patch = Path(__file__).resolve().parents[1] / "0052-agent-runtime-identity.py"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "docker"
        for relative in DOCKER_FILES:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
        manager = Path(tmp) / "hermes_cli" / "service_manager.py"
        manager.parent.mkdir(parents=True, exist_ok=True)
        manager_source_path = Path(
            os.environ.get(
                "HERMES_SERVICE_MANAGER_SOURCE",
                "/opt/hermes/hermes_cli/service_manager.py",
            )
        )
        shutil.copy2(manager_source_path, manager)

        env = {
            **os.environ,
            "HERMES_DOCKER_DIR": str(root),
            "HERMES_SERVICE_MANAGER": str(manager),
        }
        if not verify_applied:
            prerequisite = Path(__file__).resolve().parents[1] / "0046-rootless-config-migration.py"
            prerequisite_env = {**env, "HERMES_STAGE2_HOOK": str(root / "stage2-hook.sh")}
            prerequisite_result = subprocess.run(
                [sys.executable, str(prerequisite)],
                env=prerequisite_env,
                text=True,
                capture_output=True,
            )
            if prerequisite_result.returncode != 0:
                raise SystemExit(
                    f"FAIL: patch 0052 prerequisite failed:\n{prerequisite_result.stderr}"
                )
            first = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if first.returncode != 0:
                raise SystemExit(f"FAIL: patch failed:\n{first.stderr}")
            if "container runtime account renamed from hermes to agent" not in first.stdout:
                raise SystemExit("FAIL: first patch application did not transform pristine source")
            second = subprocess.run(
                [sys.executable, str(patch)], env=env, text=True, capture_output=True
            )
            if second.returncode != 0 or "already applied" not in second.stdout:
                raise SystemExit("FAIL: patch is not idempotent")

        sources = {
            relative: (root / relative).read_text(encoding="utf-8")
            for relative in DOCKER_FILES
        }
        manager_source = manager.read_text(encoding="utf-8")
        combined = "\n".join([*sources.values(), manager_source])
        forbidden = (
            "s6-setuidgid hermes",
            "hermes:hermes",
            "id -u hermes",
            "id -g hermes",
            "id -G hermes",
            "! -user hermes",
            "! -group hermes",
            'exec "$S6_SUID" hermes',
            "_HERMES_UID",
            "_HERMES_GID",
            "non-hermes UID",
            "runtime hermes UID",
            "unprivileged hermes\n",
            "supervised hermes user",
            "hermes runtime user",
            "hermes user reads",
            "hermes process with",
            "hermes is already a member",
            "so hermes can mkdir",
            "hermes user. Hosted",
            "chowned to hermes",
            "need hermes\n# ownership",
            "Owner is already hermes\n",
            "directory is hermes-\n",
            "$sock_group hermes failed",
            "`hermes` user",
        )
        remaining = [pattern for pattern in forbidden if pattern in combined]
        if remaining:
            raise SystemExit("FAIL: old account references remain: " + ", ".join(remaining))

        assertions = (
            (
                'HERMES_HOME="${HERMES_HOME:-/opt/data/.hermes}"',
                sources["stage2-hook.sh"],
            ),
            ("actual_agent_uid=$(id -u agent)", sources["stage2-hook.sh"]),
            ("s6-setuidgid agent hermes dashboard", sources["s6-rc.d/dashboard/run"]),
            ('exec "$S6_SUID" agent "$REAL" "$@"', sources["hermes-exec-shim.sh"]),
            ("drop hermes", sources["main-wrapper.sh"]),
            ("/opt/hermes", combined),
            ("HERMES_HOME", combined),
            ("HERMES_HOME:=/opt/data/.hermes", manager_source),
        )
        for expected, text in assertions:
            if expected not in text:
                raise SystemExit(f"FAIL: patched runtime is missing {expected!r}")

        for relative in DOCKER_FILES:
            syntax = subprocess.run(
                ["sh", "-n", str(root / relative)], text=True, capture_output=True
            )
            if syntax.returncode != 0:
                raise SystemExit(
                    f"FAIL: patched {relative} is invalid shell:\n{syntax.stderr}"
                )

        compiled = subprocess.run(
            [sys.executable, "-m", "py_compile", str(manager)],
            text=True,
            capture_output=True,
        )
        if compiled.returncode != 0:
            raise SystemExit(f"FAIL: patched service manager is invalid Python:\n{compiled.stderr}")

        spec = importlib.util.spec_from_file_location("patched_service_manager", manager)
        if spec is None or spec.loader is None:
            raise SystemExit("FAIL: could not load patched service manager")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module._AGENT_UID != 10000 or module._AGENT_GID != 10000:
            raise SystemExit("FAIL: generated service ownership does not use uid/gid 10000")
        run_script = module.S6ServiceManager._render_run_script("default", {})
        log_script = module.S6ServiceManager._render_log_run("default")
        if "exec s6-setuidgid agent hermes gateway run --replace" not in run_script:
            raise SystemExit("FAIL: generated gateway service does not drop to agent")
        if "s6-setuidgid hermes" in run_script + log_script:
            raise SystemExit("FAIL: generated service scripts still drop to Hermes account")
        if ': "${HERMES_HOME:=/opt/data/.hermes}"' not in log_script:
            raise SystemExit("FAIL: generated log service retains the shared-home fallback")

    print("PASS: runtime account is agent while Hermes component contracts remain intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
