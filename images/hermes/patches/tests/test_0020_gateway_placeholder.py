#!/usr/bin/env python3
"""Regression test for the trusted OpenAI gateway placeholder patch."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


GATEWAY_URL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1"
DRIVER = r'''
import sys
from hermes_cli.auth import AuthError, has_usable_secret
from hermes_cli.runtime_provider import resolve_runtime_provider

expected_ok = sys.argv[1] == "ok"
try:
    runtime = resolve_runtime_provider(
        requested="openai-api", target_model="gpt-5.6-terra"
    )
except AuthError:
    if expected_ok:
        raise
    raise SystemExit(0)

credential = runtime.get("api_key")
credential_ok = (
    callable(credential)
    or str(credential or "") in {"aws-sdk", "no-key-required"}
    or has_usable_secret(credential)
    or bool(runtime.get("command"))
)
if expected_ok:
    assert runtime["provider"] == "openai-api", runtime
    assert runtime["base_url"].rstrip("/") == sys.argv[2], runtime
    assert runtime["api_key"] == "no-key-required", runtime  # pragma: allowlist secret
    assert credential_ok, runtime
else:
    assert not credential_ok, runtime
'''


def _run(root: Path, home: Path, base_url: str, expected: str) -> None:
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: openai-api\n"
        "  default: gpt-5.6-terra\n"
        f"  base_url: {base_url}\n"
        "  api_mode: codex_responses\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{root}:/opt/hermes",
        "HERMES_HOME": str(home),
        "OPENAI_API_KEY": "none",  # pragma: allowlist secret
        "OPENAI_BASE_URL": base_url,
    }
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, expected, base_url.rstrip("/")],
        cwd="/", env=env, capture_output=True, text=True,
    )
    if proc.returncode:
        raise SystemExit(
            f"FAIL: runtime probe for {base_url} returned {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def main() -> int:
    runtime_spec = importlib.util.find_spec("hermes_cli.runtime_provider")
    if runtime_spec is None or not runtime_spec.origin:
        raise SystemExit("cannot locate installed hermes_cli/runtime_provider.py")
    installed = Path(runtime_spec.origin)
    patch = Path(__file__).resolve().parents[1] / "0020-gateway-placeholder-credential.py"
    live_before = installed.read_text()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        package = root / "hermes_cli"
        package.mkdir(parents=True)
        installed_package = installed.parent
        for child in installed_package.iterdir():
            target = package / child.name
            if child.name == "runtime_provider.py":
                shutil.copy2(child, target)
            else:
                target.symlink_to(child, target_is_directory=child.is_dir())

        env = {**os.environ, "PYTHONPATH": f"{root}:/opt/hermes"}
        for expected_text in ("pool-entry OpenAI", "already applied"):
            proc = subprocess.run(
                [sys.executable, str(patch)], cwd="/", env=env,
                capture_output=True, text=True,
            )
            if proc.returncode or expected_text not in proc.stdout:
                raise SystemExit(
                    f"FAIL: patch run did not report {expected_text!r}\n"
                    f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
                )

        _run(root, Path(tmp) / "gateway-home", GATEWAY_URL, "ok")
        _run(root, Path(tmp) / "openai-home", "https://api.openai.com/v1", "fail")
        _run(root, Path(tmp) / "external-home", "https://untrusted.example/v1", "fail")

    if installed.read_text() != live_before:
        raise SystemExit("FAIL: test mutated the installed Hermes tree")
    print("PASS: only the trusted Agentgateway OpenAI route accepts the placeholder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
