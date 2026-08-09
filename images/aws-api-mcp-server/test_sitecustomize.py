#!/usr/bin/env python3
"""Regression tests for the AWS API MCP compatibility-patch startup gate."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
PATCH_DIR = REPO / "images" / "aws-api-mcp-server"
DOCKERFILE = PATCH_DIR / "Dockerfile"


def write_upstream_stub(root: pathlib.Path, server_source: str) -> None:
    package = root / "awslabs" / "aws_api_mcp_server"
    package.mkdir(parents=True)
    (root / "awslabs" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "server.py").write_text(textwrap.dedent(server_source), encoding="utf-8")


def run_with_stub(root: pathlib.Path, script: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"PYTHONPATH": os.pathsep.join((str(PATCH_DIR), str(root)))}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


MARKER_COMPATIBLE_HELPER = """
    def check_security_policy(ir, index, ctx):
        return None

    def call_aws_helper(cli_command, ctx, max_results=None, credentials=None, default_region=None):
        is_awscli_customization = False
        is_help_operation = False
        if is_awscli_customization:
            return execute_awscli_customization(cli_command)
        return interpret_command(
            cli_command=cli_command,
            max_results=max_results,
            credentials=credentials,
            default_region_override=default_region,
        )
"""


class AwsPatchStartupGateTests(unittest.TestCase):
    def test_incompatible_upstream_fails_explicit_build_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            write_upstream_stub(root, "def call_aws_helper():\n    return 'upstream drifted'\n")
            result = run_with_stub(root, "import sitecustomize")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("aws-api-mcp compatibility patch failed", result.stderr)

    def test_marker_compatible_upstream_replaces_helper_with_async_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            write_upstream_stub(root, MARKER_COMPATIBLE_HELPER)
            result = run_with_stub(
                root,
                """
                import asyncio
                from awslabs.aws_api_mcp_server import server

                assert asyncio.iscoroutinefunction(server.call_aws_helper)
                assert server.call_aws_helper.__module__ == "sitecustomize"
                """,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_async_security_policy_helper_fails_explicit_build_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = MARKER_COMPATIBLE_HELPER.replace(
                "def check_security_policy(ir, index, ctx):",
                "async def check_security_policy(ir, index, ctx):",
            )
            write_upstream_stub(root, source)
            result = run_with_stub(root, "import sitecustomize")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("unexpected async check_security_policy", result.stderr)

    def test_patch_offloads_blocking_aws_execution_without_freezing_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            write_upstream_stub(
                root,
                textwrap.dedent(
                    """
                    import threading
                    from types import SimpleNamespace

                    class AwsApiMcpError(Exception):
                        def as_failure(self):
                            return SimpleNamespace(reason=str(self))

                    class CommandValidationError(AwsApiMcpError):
                        pass

                    class NoCredentialsError(Exception):
                        pass

                    class Logger:
                        def info(self, *args, **kwargs):
                            pass

                    logger = Logger()
                    READ_OPERATIONS_INDEX = None
                    READ_OPERATIONS_ONLY_MODE = False
                    REQUIRE_MUTATION_CONSENT = False
                    READ_ONLY_KEY = "READ_ONLY"
                    PolicyDecision = SimpleNamespace(DENY="deny", ELICIT="elicit")
                    started = threading.Event()
                    release = threading.Event()

                    def translate_cli_to_ir(cli_command):
                        return SimpleNamespace(
                            command=SimpleNamespace(
                                service_name="ec2",
                                operation_cli_name="describe-instances",
                                is_help_operation=False,
                                is_awscli_customization=False,
                            )
                        )

                    def validate(ir):
                        return SimpleNamespace(validation_failed=False, model_dump_json=lambda: "{}")

                    def interpret_command(**kwargs):
                        started.set()
                        release.wait(timeout=2)
                        return "done"

                    def execute_awscli_customization(*args, **kwargs):
                        raise AssertionError("unexpected customization path")
                    """
                )
                + "\n"
                + textwrap.dedent(MARKER_COMPATIBLE_HELPER),
            )
            result = run_with_stub(
                root,
                """
                import asyncio
                import threading
                import time
                from awslabs.aws_api_mcp_server import server

                class Context:
                    async def error(self, message):
                        raise AssertionError(message)

                async def main():
                    loop_progressed = asyncio.Event()

                    async def prove_loop_progress():
                        await asyncio.sleep(0)
                        loop_progressed.set()

                    release_timer = threading.Timer(2, server.release.set)
                    release_timer.start()
                    try:
                        work = asyncio.create_task(server.call_aws_helper("ec2 describe-instances", Context()))
                        progress = asyncio.create_task(prove_loop_progress())
                        assert await asyncio.to_thread(server.started.wait, 2)
                        await progress
                        assert loop_progressed.is_set()
                        assert not server.release.is_set()
                        server.release.set()
                        assert await work == "done"
                    finally:
                        server.release.set()
                        release_timer.cancel()

                asyncio.run(main())
                """,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dockerfile_explicitly_imports_patch_during_build(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("RUN PYTHONPATH=/opt/patch python -c 'import sitecustomize'", dockerfile)


if __name__ == "__main__":
    unittest.main()
