#!/usr/bin/env python3
"""Focused regression tests for interactive Vicegerent MCP CLI helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch


MODULE_PATH = Path(__file__).with_name("vicegerent_mcp.py")
SPEC = importlib.util.spec_from_file_location("vicegerent_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
vicegerent_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vicegerent_mcp)


class InternalKubeconfigTests(unittest.TestCase):
    def test_generated_kubeconfig_is_owner_readable_only(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="apiVersion: v1\n", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed),
        ):
            path = vicegerent_mcp.write_internal_kubeconfig("vicegerent", Path(directory))

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(encoding="utf-8"), completed.stdout)


class WorkloadRecoveryTests(unittest.TestCase):
    def test_error_recovery_stops_before_starting(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "firecrawl", "enabled": True}]}
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                vicegerent_mcp,
                "list_workloads",
                side_effect=[{"firecrawl": "error"}, {"firecrawl": "stopped"}, {"firecrawl": "running"}],
            ),
            patch.object(vicegerent_mcp, "thv", return_value=completed) as thv,
            patch.object(vicegerent_mcp.time, "sleep"),
        ):
            vicegerent_mcp.wait_for_workloads_running(config, Path(directory), timeout=30)

        self.assertEqual(thv.call_args_list, [call("stop", "firecrawl"), call("start", "firecrawl")])

    def test_error_recovery_does_not_issue_restart(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "github", "enabled": True}]}
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                vicegerent_mcp,
                "list_workloads",
                side_effect=[{"github": "error"}, {"github": "running"}],
            ),
            patch.object(vicegerent_mcp, "thv", return_value=completed) as thv,
            patch.object(vicegerent_mcp.time, "sleep"),
        ):
            vicegerent_mcp.wait_for_workloads_running(config, Path(directory), timeout=30)

        self.assertEqual(thv.call_args_list, [call("stop", "github")])


class HealthWatchTests(unittest.TestCase):
    def test_running_workload_clears_a_prior_watcher_notification(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "github", "enabled": True}]}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "list_workloads", return_value={"github": "running"}),
            patch.object(vicegerent_mcp, "_notify_clear") as notify_clear,
            patch.object(vicegerent_mcp.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                vicegerent_mcp.health_watch(Path(directory), interval=1)

        self.assertEqual(
            notify_clear.call_args_list,
            [call("vicegerent-mcp-github"), call(vicegerent_mcp._AWS_CRED_GROUP)],
        )

    def test_healthy_credentials_clear_a_prior_watcher_notification(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "aws", "enabled": True}]}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "list_workloads", return_value={"aws": "running"}),
            patch.object(vicegerent_mcp, "server_param", return_value=""),
            patch.object(vicegerent_mcp, "_aws_cred_status", return_value=None),
            patch.object(vicegerent_mcp, "_notify_clear") as notify_clear,
            patch.object(vicegerent_mcp.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                vicegerent_mcp.health_watch(Path(directory), interval=1)

        self.assertEqual(
            notify_clear.call_args_list,
            [call("vicegerent-mcp-aws"), call(vicegerent_mcp._AWS_CRED_GROUP)],
        )


class NotificationTests(unittest.TestCase):
    def test_notification_message_is_not_timestamped(self) -> None:
        with patch.object(vicegerent_mcp.subprocess, "run") as run:
            vicegerent_mcp._notify("Title", "Message", group="test")

        self.assertIn("Message", run.call_args.args[0])
        self.assertNotIn("Message [", run.call_args.args[0])

    def test_aws_expiry_message_shows_only_the_local_12_hour_time(self) -> None:
        now = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)
        expires_at = datetime(2026, 8, 8, 4, 27, 36, tzinfo=timezone.utc)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        class FakeDatetime:
            @classmethod
            def now(cls, tz: timezone) -> datetime:
                return now

        with (
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed),
            patch.object(vicegerent_mcp, "datetime", FakeDatetime),
            patch.object(vicegerent_mcp, "_profile_sso_start_url", return_value="https://example.com/start"),
            patch.object(vicegerent_mcp, "_sso_token_expiry", return_value=expires_at),
        ):
            result = vicegerent_mcp._aws_cred_status("", warning_secs=60 * 60 * 12)

        self.assertIsNotNone(result)
        self.assertEqual(result[2], f"Expires at {expires_at.astimezone():%I:%M %p}.")


class StoreHiddenSecretTests(unittest.TestCase):
    def test_value_is_prefixed_and_passed_only_on_stdin(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value="token"),
            patch.object(vicegerent_mcp, "_thv_path", return_value="/usr/bin/thv"),
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed) as run,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = vicegerent_mcp._store_hidden_secret("elastic_key", "Elastic API key", "ApiKey ")

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            ["/usr/bin/thv", "secret", "set", "elastic_key"],
            input="ApiKey token",
            text=True,
            capture_output=True,
        )
        self.assertNotIn("token", stdout.getvalue() + stderr.getvalue())

    def test_toolhive_error_output_is_not_relayed(self) -> None:
        secret = "do-not-print-this"  # pragma: allowlist secret
        completed = subprocess.CompletedProcess([], 1, stdout=secret, stderr=secret)
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value=secret),
            patch.object(vicegerent_mcp, "_thv_path", return_value="thv"),
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            result = vicegerent_mcp._store_hidden_secret("api_key", "API key")

        self.assertEqual(result, 1)
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())

    def test_blank_input_does_not_replace_existing_secret(self) -> None:
        with (
            patch.object(vicegerent_mcp.getpass, "getpass", return_value=""),
            patch.object(vicegerent_mcp.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = vicegerent_mcp._store_hidden_secret("api_key", "API key")

        self.assertIsNone(result)
        run.assert_not_called()


class ConfigureTests(unittest.TestCase):
    def test_every_configured_secret_has_a_visible_label(self) -> None:
        config_path = MODULE_PATH.with_name("toolhive-servers.json")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        unnamed = []
        secret_prompts = []
        for server in config["servers"]:
            for secret in server.get("secrets", []):
                label = secret.get("prompt")
                if not label:
                    unnamed.append(f"{server['name']}.{secret['name']}")
                else:
                    secret_prompts.append(label)
            for param in server.get("params", []):
                if param.get("secret") and not param.get("prompt"):
                    unnamed.append(f"{server['name']}.{param['name']}")

        self.assertEqual(unnamed, [])
        self.assertEqual(len(secret_prompts), len(set(secret_prompts)))

    def test_labels_each_configured_secret(self) -> None:
        config = {
            "group": "vicegerent",
            "servers": [{
                "name": "grafana_secondary",
                "secrets": [
                    {"name": "grafana_secondary_url", "prompt": "Secondary Grafana URL"},
                    {
                        "name": "grafana_secondary_service_account_token",
                        "prompt": "Secondary Grafana service account token",
                    },
                ],
            }],
        }
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "load_server_params", return_value={}),
            patch.object(vicegerent_mcp, "list_workloads", return_value={}),
            patch.object(vicegerent_mcp, "_prompt_yn", return_value=True) as prompt_yn,
            patch.object(vicegerent_mcp, "thv", return_value=completed),
            patch.object(vicegerent_mcp, "_store_hidden_secret", return_value=0) as store,
            patch.object(vicegerent_mcp, "save_server_state"),
            patch.object(vicegerent_mcp, "save_server_params"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = vicegerent_mcp.configure(Path(directory))

        self.assertEqual(result, 0)
        prompt_yn.assert_has_calls([
            call("   Secondary Grafana URL is already configured — replace it?", default=False),
            call(
                "   Secondary Grafana service account token is already configured — replace it?",
                default=False,
            ),
        ])
        self.assertEqual(
            store.call_args_list,
            [
                call("grafana_secondary_url", "Secondary Grafana URL"),
                call(
                    "grafana_secondary_service_account_token",
                    "Secondary Grafana service account token",
                ),
            ],
        )


class WorkloadLogProcessTests(unittest.TestCase):
    def test_follows_named_workload_logs(self) -> None:
        with (
            patch.object(vicegerent_mcp, "_thv_path", return_value="/opt/homebrew/bin/thv"),
            patch.object(vicegerent_mcp.subprocess, "Popen") as popen,
        ):
            vicegerent_mcp.workload_log_process("gitlab")

        popen.assert_called_once_with(
            ["/opt/homebrew/bin/thv", "logs", "gitlab", "--follow"],
            stdout=vicegerent_mcp.subprocess.PIPE,
            stderr=vicegerent_mcp.subprocess.STDOUT,
            text=True,
        )


class VmcpEnvironmentTests(unittest.TestCase):
    def test_localhost_compatibility_bypass_is_scoped_to_gateway_vmcp(self) -> None:
        gateway = vicegerent_mcp.vmcp_environment("/test/bin", allow_non_loopback_hosts=True)
        operator = vicegerent_mcp.vmcp_environment("/test/bin")

        self.assertEqual(gateway["MCPGODEBUG"], "disablelocalhostprotection=1")
        self.assertNotIn("MCPGODEBUG", operator)
        self.assertEqual(operator, {"PATH": "/test/bin", "HOME": str(Path.home())})


class OperatorVmcpTests(unittest.TestCase):
    def _generate_scoped(self, runtime_dir: Path) -> Path:
        init_yaml = """\
name: vicegerent-vmcp
groupRef: vicegerent
backends:
  - name: gitlab
    url: http://127.0.0.1:9001/mcp
    transport: streamable-http
"""

        def fake_thv(*args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("vmcp", "init"):
                Path(args[args.index("--output") + 1]).write_text(init_yaml, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        config = {
            "group": "vicegerent",
            "servers": [{"name": "gitlab", "tools": ["get_project"]}],
        }
        with (
            patch.object(vicegerent_mcp, "thv", side_effect=fake_thv),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return vicegerent_mcp.generate_vmcp_config(config, runtime_dir, validate=False)

    def test_operator_config_reuses_backends_without_tool_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            scoped_path = self._generate_scoped(runtime_dir)
            operator_path = vicegerent_mcp.generate_operator_vmcp_config(
                scoped_path, runtime_dir, validate=False,
            )
            scoped = json.loads(scoped_path.read_text(encoding="utf-8"))
            operator = json.loads(operator_path.read_text(encoding="utf-8"))

        self.assertEqual(
            scoped["aggregation"]["tools"],  # type: ignore[index]
            [{"workload": "gitlab", "filter": ["get_project"]}],
        )
        self.assertNotIn("tools", operator["aggregation"])  # type: ignore[operator]
        self.assertEqual(operator["name"], "vicegerent-vmcp-operator")
        self.assertEqual(operator["backends"], scoped["backends"])

    def test_supervisor_block_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = vicegerent_mcp.runtime_paths(Path(directory))
            common = (
                paths, Path("ghostshell"), {}, "thv vmcp serve scoped", {},
                "thv vmcp serve operator --optimizer", {}, Path("rcloneshell"), {},
                "health-watch", {},
            )
            disabled = vicegerent_mcp.build_supervisord_conf(*common)
            enabled = vicegerent_mcp.build_supervisord_conf(*common, operator_vmcp=True)

        self.assertNotIn("[program:operator-vmcp]", disabled)
        self.assertIn("[program:operator-vmcp]", enabled)
        self.assertIn("command=thv vmcp serve operator --optimizer", enabled)
        self.assertIn("stdout_logfile=" + str(paths["logs"] / "operator-vmcp.log"), enabled)

    def test_start_flag_is_explicit(self) -> None:
        parser = vicegerent_mcp.build_parser()
        self.assertFalse(parser.parse_args(["start"]).operator_vmcp)
        self.assertTrue(parser.parse_args(["start", "--operator-vmcp"]).operator_vmcp)

    def test_operator_port_must_be_valid_and_distinct(self) -> None:
        with patch.dict(os.environ, {"OPERATOR_VMCP_PORT": "not-a-port"}):
            with self.assertRaisesRegex(SystemExit, "must be an integer"):
                vicegerent_mcp.operator_vmcp_port()
        with patch.dict(os.environ, {"OPERATOR_VMCP_PORT": "70000"}):
            with self.assertRaisesRegex(SystemExit, "between 1 and 65535"):
                vicegerent_mcp.operator_vmcp_port()
        with self.assertRaisesRegex(SystemExit, "conflicts with scoped vMCP"):
            vicegerent_mcp.validate_operator_vmcp_port(4483, {"scoped vMCP": 4483})

    def test_port_conflict_fails_before_starting_any_workload(self) -> None:
        config = {"group": "vicegerent", "vmcp_port": 4483, "servers": []}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"OPERATOR_VMCP_PORT": "4483"}),
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "ensure_ghostunnel_material") as ensure_material,
            patch.object(vicegerent_mcp, "run_workloads") as run_workloads,
        ):
            with self.assertRaisesRegex(SystemExit, "conflicts with scoped vMCP"):
                vicegerent_mcp.start_stack(Path(directory), operator_vmcp=True)

        ensure_material.assert_not_called()
        run_workloads.assert_not_called()

    def test_operator_opt_out_stops_an_orphaned_listener(self) -> None:
        with (
            patch.object(vicegerent_mcp, "_addr_reachable", return_value=True),
            patch.object(vicegerent_mcp, "_kill_addr_listeners", return_value=[123]) as kill,
        ):
            self.assertEqual(vicegerent_mcp._stop_disabled_operator_vmcp(False, 4484), [123])
            self.assertEqual(vicegerent_mcp._stop_disabled_operator_vmcp(True, 4484), [])

        kill.assert_called_once_with("127.0.0.1:4484")

    def test_stray_supervisor_cleanup_preserves_the_reachable_instance(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="10\n20\n", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp.subprocess, "run", return_value=completed),
            patch.object(vicegerent_mcp, "_terminate_pids") as terminate,
        ):
            killed = vicegerent_mcp._kill_stray_supervisord(
                vicegerent_mcp.runtime_paths(Path(directory)),
                preserve_pids=frozenset({20}),
            )

        self.assertEqual(killed, [10])
        terminate.assert_called_once_with([10], 10.0)


if __name__ == "__main__":
    unittest.main()
