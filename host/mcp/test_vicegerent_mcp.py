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


class HostPackagePreflightTests(unittest.TestCase):
    def test_current_packages_skip_apply(self) -> None:
        current = subprocess.CompletedProcess([], 0, stdout="all current\n", stderr="")
        with (
            patch.object(vicegerent_mcp.subprocess, "run", return_value=current) as run,
            patch.object(vicegerent_mcp, "_ui_ok"),
        ):
            vicegerent_mcp.ensure_host_packages_current()

        run.assert_called_once_with(
            [
                vicegerent_mcp.sys.executable,
                str(vicegerent_mcp.HOST_PACKAGE_RECONCILER),
                "check",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_drift_forces_noninteractive_apply(self) -> None:
        drift = subprocess.CompletedProcess([], 1, stdout="DRIFT toolhive\n", stderr="")
        applied = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            patch.object(vicegerent_mcp.subprocess, "run", side_effect=[drift, applied]) as run,
            patch.object(vicegerent_mcp, "_ui_warn"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            vicegerent_mcp.ensure_host_packages_current()

        self.assertEqual(run.call_args_list[1], call([
            vicegerent_mcp.sys.executable,
            str(vicegerent_mcp.HOST_PACKAGE_RECONCILER),
            "apply",
            "--yes",
        ], check=False))

    def test_failed_apply_aborts_startup(self) -> None:
        drift = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with (
            patch.object(vicegerent_mcp.subprocess, "run", side_effect=[drift, failed]),
            patch.object(vicegerent_mcp, "_ui_warn"),
        ):
            with self.assertRaisesRegex(SystemExit, "MCP startup aborted"):
                vicegerent_mcp.ensure_host_packages_current()

    def test_mcp_start_runs_package_preflight(self) -> None:
        args = type("Args", (), {
            "runtime_dir": Path("/runtime"),
            "servers_config": Path("/servers.json"),
            "ghostshell": Path("/ghostshell"),
            "listen": "127.0.0.1:8453",
            "allow_cn": "agentgateway",
            "skip_workloads": False,
            "caffeinate": False,
            "operator_vmcp": False,
        })()
        with (
            patch.object(vicegerent_mcp, "ensure_host_packages_current") as preflight,
            patch.object(vicegerent_mcp, "start_stack", return_value=0) as start,
        ):
            self.assertEqual(vicegerent_mcp.cmd_start(args), 0)

        preflight.assert_called_once_with()
        start.assert_called_once()


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
    def test_running_workload_retries_clearing_a_prior_watcher_notification(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "github", "enabled": True}]}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "list_workloads", return_value={"github": "running"}),
            patch.object(vicegerent_mcp, "reconcile_vmcp_membership"),
            patch.object(vicegerent_mcp, "_notify_clear") as notify_clear,
            patch.object(vicegerent_mcp.time, "sleep", side_effect=[None, KeyboardInterrupt]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                vicegerent_mcp.health_watch(Path(directory), interval=1)

        self.assertEqual(
            notify_clear.call_args_list,
            [
                call("vicegerent-mcp-github"),
                call(vicegerent_mcp._AWS_CRED_GROUP),
                call("vicegerent-mcp-github"),
                call(vicegerent_mcp._AWS_CRED_GROUP),
            ],
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
            patch.object(vicegerent_mcp, "reconcile_vmcp_membership"),
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

    def test_unhealthy_workload_reposts_until_it_recovers(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "github", "enabled": True}]}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(
                vicegerent_mcp,
                "list_workloads",
                side_effect=[{"github": "error"}, {"github": "error"}, {"github": "running"}],
            ),
            patch.object(vicegerent_mcp, "reconcile_vmcp_membership"),
            patch.object(vicegerent_mcp, "_notify") as notify,
            patch.object(vicegerent_mcp, "_notify_clear") as notify_clear,
            patch.object(vicegerent_mcp.time, "sleep", side_effect=[None, None, KeyboardInterrupt]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                vicegerent_mcp.health_watch(Path(directory), interval=1)

        self.assertEqual(
            notify.call_args_list,
            [
                call(
                    "MCP backend down: github",
                    "Run vicegerent start to bring it back.",
                    group="vicegerent-mcp-github",
                ),
                call(
                    "MCP backend down: github",
                    "Run vicegerent start to bring it back.",
                    group="vicegerent-mcp-github",
                ),
            ],
        )
        self.assertIn(call("vicegerent-mcp-github"), notify_clear.call_args_list)

    def test_expired_credentials_repost_until_refreshed(self) -> None:
        config = {"group": "vicegerent", "servers": [{"name": "aws", "enabled": True}]}
        expired = (
            "aws-expired",
            "AWS credentials expired",
            "Refresh host-side (e.g. aws sso login), then run vicegerent start.",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "list_workloads", return_value={"aws": "running"}),
            patch.object(vicegerent_mcp, "server_param", return_value=""),
            patch.object(vicegerent_mcp, "_aws_cred_status", side_effect=[expired, expired, None]),
            patch.object(vicegerent_mcp, "reconcile_vmcp_membership"),
            patch.object(vicegerent_mcp, "_notify") as notify,
            patch.object(vicegerent_mcp, "_notify_clear") as notify_clear,
            patch.object(vicegerent_mcp.time, "sleep", side_effect=[None, None, KeyboardInterrupt]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                vicegerent_mcp.health_watch(Path(directory), interval=1)

        self.assertEqual(
            notify.call_args_list,
            [
                call(expired[1], expired[2], group=vicegerent_mcp._AWS_CRED_GROUP),
                call(expired[1], expired[2], group=vicegerent_mcp._AWS_CRED_GROUP),
            ],
        )
        self.assertIn(call(vicegerent_mcp._AWS_CRED_GROUP), notify_clear.call_args_list)


class NotificationTests(unittest.TestCase):
    def test_notification_message_is_not_timestamped(self) -> None:
        with patch.object(vicegerent_mcp.subprocess, "run") as run:
            vicegerent_mcp._notify("Title", "Message", group="test")

        self.assertIn("Message", run.call_args.args[0])
        self.assertNotIn("Message [", run.call_args.args[0])
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            vicegerent_mcp.NOTIFIER_TIMEOUT_SECS,
        )
        self.assertIn(str(vicegerent_mcp.NOTIFIER_BINARY), run.call_args.args[0])
        self.assertIn("post", run.call_args.args[0])
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["post", "test", "Title", "Message"],
        )

    def test_stuck_notification_post_does_not_block_the_watcher(self) -> None:
        timeout = subprocess.TimeoutExpired(["vicegerent-notifier"], 5)
        with patch.object(vicegerent_mcp.subprocess, "run", side_effect=timeout):
            vicegerent_mcp._notify("Title", "Message", group="test")

    def test_stuck_notification_removal_does_not_block_the_watcher(self) -> None:
        timeout = subprocess.TimeoutExpired(["vicegerent-notifier"], 5)
        with patch.object(vicegerent_mcp.subprocess, "run", side_effect=timeout) as run:
            vicegerent_mcp._notify_clear("test")

        self.assertIn("remove", run.call_args.args[0])
        self.assertIn("test", run.call_args.args[0])

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
            "servers": [{"name": "gitlab", "enabled": True, "tools": ["get_project"]}],
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

    def test_scoped_config_excludes_disabled_backends_from_backends_and_filters(self) -> None:
        init_yaml = """\\
name: vicegerent-vmcp
groupRef: vicegerent
backends:
  - name: enabled
    url: http://127.0.0.1:9001/mcp
    transport: streamable-http
  - name: disabled
    url: http://127.0.0.1:9002/mcp
    transport: streamable-http
"""

        def fake_thv(*args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("vmcp", "init"):
                Path(args[args.index("--output") + 1]).write_text(init_yaml, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        config = {
            "group": "vicegerent",
            "servers": [
                {"name": "enabled", "enabled": True, "tools": ["get_enabled"]},
                {"name": "disabled", "enabled": False, "tools": ["get_disabled"]},
            ],
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "thv", side_effect=fake_thv),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            scoped_path = vicegerent_mcp.generate_vmcp_config(config, Path(directory), validate=False)
            scoped = json.loads(scoped_path.read_text(encoding="utf-8"))

        self.assertEqual([backend["name"] for backend in scoped["backends"]], ["enabled"])
        self.assertEqual(
            scoped["aggregation"]["tools"],
            [{"workload": "enabled", "filter": ["get_enabled"]}],
        )

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

        kill.assert_called_once_with("127.0.0.1:4484", name="operator-vmcp")

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


class DurableStateTests(unittest.TestCase):
    def test_legacy_runtime_state_migrates_once_to_durable_versioned_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_dir = root / "runtime"
            durable_path = root / "state" / "servers-state.json"
            runtime_dir.mkdir()
            (runtime_dir / "servers-state.json").write_text(
                json.dumps({"enabled": {"github": True}, "params": {"github": {"url": "https://example.test"}}}),
                encoding="utf-8",
            )
            with patch.object(vicegerent_mcp, "durable_state_path", return_value=durable_path):
                state = vicegerent_mcp._read_state(runtime_dir)

            self.assertEqual(state["version"], vicegerent_mcp.STATE_VERSION)
            self.assertEqual(state["enabled"], {"github": True})
            self.assertTrue(durable_path.exists())
            self.assertFalse((runtime_dir / "servers-state.json").exists())

    def test_malformed_durable_state_fails_visibly_without_resetting_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_path = Path(directory) / "servers-state.json"
            durable_path.write_text("not json", encoding="utf-8")
            with patch.object(vicegerent_mcp, "durable_state_path", return_value=durable_path):
                with self.assertRaisesRegex(SystemExit, "invalid durable MCP state"):
                    vicegerent_mcp._read_state(Path(directory) / "runtime")

    def test_corrupt_primary_recovers_previous_durable_intent_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_path = Path(directory) / "servers-state.json"
            previous_path = durable_path.with_suffix(".json.previous")
            durable_path.write_text('{"version": 1, "enabled": ', encoding="utf-8")
            previous_path.write_text(
                json.dumps({"version": 1, "enabled": {"github": True}, "params": {}, "fingerprints": {}}),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with (
                patch.object(vicegerent_mcp, "durable_state_path", return_value=durable_path),
                contextlib.redirect_stderr(stderr),
            ):
                state = vicegerent_mcp._read_state(Path(directory) / "runtime")

            self.assertEqual(state["enabled"], {"github": True})
            self.assertEqual(json.loads(durable_path.read_text(encoding="utf-8"))["enabled"], {"github": True})
            self.assertIn("Recovered durable MCP state", stderr.getvalue())

    def test_partial_write_does_not_replace_existing_durable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_path = Path(directory) / "servers-state.json"
            original = {"version": 1, "enabled": {"github": True}, "params": {}, "fingerprints": {}}
            durable_path.write_text(json.dumps(original), encoding="utf-8")
            replacement = {"version": 1, "enabled": {"github": False}, "params": {}, "fingerprints": {}}
            with patch.object(vicegerent_mcp.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    vicegerent_mcp._write_state_to(durable_path, replacement)

            self.assertEqual(json.loads(durable_path.read_text(encoding="utf-8")), original)
            self.assertEqual(json.loads(durable_path.with_suffix(".json.previous").read_text(encoding="utf-8")), original)

    def test_missing_optional_legacy_fields_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_path = Path(directory) / "servers-state.json"
            durable_path.write_text(json.dumps({"version": 1, "enabled": {"github": True}}), encoding="utf-8")
            with patch.object(vicegerent_mcp, "durable_state_path", return_value=durable_path):
                state = vicegerent_mcp._read_state(Path(directory) / "runtime")

            self.assertEqual(state, {"version": 1, "enabled": {"github": True}, "params": {}, "fingerprints": {}})

    def test_malformed_values_and_incompatible_versions_fail_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durable_path = Path(directory) / "servers-state.json"
            runtime_dir = Path(directory) / "runtime"
            with patch.object(vicegerent_mcp, "durable_state_path", return_value=durable_path):
                durable_path.write_text(json.dumps({"version": 1, "enabled": {"github": "yes"}}), encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "enabled must map names to booleans"):
                    vicegerent_mcp._read_state(runtime_dir)
                durable_path.write_text(json.dumps({"version": 999, "enabled": {}, "params": {}, "fingerprints": {}}), encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "unsupported durable MCP state version"):
                    vicegerent_mcp._read_state(runtime_dir)


class DiscoveryAndOwnershipTests(unittest.TestCase):
    def test_reconcile_repairs_stale_tool_filters_when_backend_membership_matches(self) -> None:
        config = {
            "group": "vicegerent",
            "servers": [{"name": "github", "enabled": True, "tools": ["new_tool"]}],
        }
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            paths = vicegerent_mcp.runtime_paths(runtime_dir)
            paths["vmcp_config"].write_text(json.dumps({
                "backends": [{"name": "github"}],
                "aggregation": {"tools": [{"workload": "github", "filter": ["old_tool"]}]},
            }), encoding="utf-8")
            with (
                patch.object(vicegerent_mcp, "discover_workloads", return_value=vicegerent_mcp.WorkloadDiscovery({"github": "running"})),
                patch.object(vicegerent_mcp, "load_server_state", return_value={}),
                patch.object(vicegerent_mcp, "generate_vmcp_config") as generate,
                patch.object(vicegerent_mcp, "supervisorctl", return_value=completed) as supervisor,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                reconciled = vicegerent_mcp.reconcile_vmcp_membership(config, runtime_dir)

        self.assertTrue(reconciled)
        generate.assert_called_once_with(config, runtime_dir)
        supervisor.assert_called_once_with("restart", "vmcp", runtime_dir=runtime_dir)

    def test_workload_discovery_error_is_preserved(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable")
        with patch.object(vicegerent_mcp, "thv", return_value=failed):
            result = vicegerent_mcp.discover_workloads("vicegerent")

        self.assertFalse(result.ok)
        self.assertIn("daemon unavailable", result.error)

    def test_unknown_listener_is_not_adopted_or_terminated(self) -> None:
        with (
            patch.object(vicegerent_mcp, "_listener_pids", return_value=[123]),
            patch.object(vicegerent_mcp, "_managed_listener_pids", return_value=set()),
            patch.object(vicegerent_mcp, "_terminate_pids") as terminate,
        ):
            with self.assertRaisesRegex(SystemExit, "unknown listener"):
                vicegerent_mcp.require_managed_listener("127.0.0.1:4483", "vmcp")

        terminate.assert_not_called()

    def test_orphaned_ghostunnel_binary_is_recognized_after_ghostshell_backgrounds_it(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="/opt/homebrew/bin/ghostunnel server --listen 127.0.0.1:8453", stderr="")
        with patch.object(vicegerent_mcp.subprocess, "run", return_value=completed):
            self.assertTrue(vicegerent_mcp._listener_matches_service(123, "ghostunnel"))

    def test_orphaned_rclone_binary_is_recognized_after_wrapper_execs_it(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="/opt/homebrew/bin/rclone serve s3 --addr 127.0.0.1:9899 /backups", stderr="")
        with patch.object(vicegerent_mcp.subprocess, "run", return_value=completed):
            self.assertTrue(vicegerent_mcp._listener_matches_service(123, "rclone-s3"))

    def test_listener_identity_rejects_an_unrelated_process_with_service_words_in_argv(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="python3 -c 'print(\"ghostunnel server rclone serve s3\")'", stderr="")
        with patch.object(vicegerent_mcp.subprocess, "run", return_value=completed):
            self.assertFalse(vicegerent_mcp._listener_matches_service(123, "ghostunnel"))
            self.assertFalse(vicegerent_mcp._listener_matches_service(123, "rclone-s3"))

    def test_stop_stops_every_nonterminal_workload_state_before_terminal_verification(self) -> None:
        config = {"group": "vicegerent", "servers": []}
        first = vicegerent_mcp.WorkloadDiscovery({
            "running": "running",
            "unauthenticated": "unauthenticated",
            "error": "error",
            "starting": "starting",
            "stopped": "stopped",
            "removed": "removed",
        })
        final = vicegerent_mcp.WorkloadDiscovery({
            "running": "stopped",
            "unauthenticated": "stopped",
            "error": "removed",
            "starting": "stopped",
            "stopped": "stopped",
            "removed": "removed",
        })
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "discover_workloads", side_effect=[first, final]),
            patch.object(vicegerent_mcp, "thv", return_value=completed) as thv,
            patch.object(vicegerent_mcp, "is_supervisor_running", return_value=False),
            patch.object(vicegerent_mcp, "_kill_stray_supervisord", return_value=[]),
            patch.object(vicegerent_mcp, "_kill_addr_listeners", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(vicegerent_mcp.stop_stack(Path(directory)), 0)

        self.assertEqual(
            {call.args for call in thv.call_args_list},
            {("stop", "running"), ("stop", "unauthenticated"), ("stop", "error"), ("stop", "starting")},
        )

    def test_status_fails_for_an_unknown_reachable_listener(self) -> None:
        tables = []

        class FakeTable:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.rows = []
                tables.append(self)

            def add_column(self, *args: object, **kwargs: object) -> None:
                pass

            def add_row(self, *row: str) -> None:
                self.rows.append(row)

        class FakeConsole:
            def print(self, table: FakeTable) -> None:
                pass

        config = {"group": "vicegerent", "servers": [], "vmcp_port": 4483}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vicegerent_mcp, "_require_rich", return_value=(FakeConsole(), FakeTable)),
            patch.object(vicegerent_mcp, "load_servers_config", return_value=config),
            patch.object(vicegerent_mcp, "list_workloads", return_value={}),
            patch.object(vicegerent_mcp, "load_server_state", return_value={}),
            patch.object(vicegerent_mcp, "get_supervisor_states", return_value={}),
            patch.object(vicegerent_mcp, "_addr_reachable", return_value=True),
            patch.object(vicegerent_mcp, "require_managed_listener", side_effect=SystemExit("unknown listener on 127.0.0.1:4483")),
            patch.object(vicegerent_mcp, "_ui_error") as error,
        ):
            rc = vicegerent_mcp.status(Path(directory))

        self.assertEqual(rc, 1)
        self.assertIn(("vmcp", "[red]UNKNOWN[/red]"), tables[1].rows)
        error.assert_any_call("unknown listener on 127.0.0.1:4483")


if __name__ == "__main__":
    unittest.main()
