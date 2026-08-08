#!/usr/bin/env python3
"""Regression tests for installer convergence and CSI recovery without a cluster."""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import os
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
RECONCILE_LIB = REPO_ROOT / "scripts/install/lib/reconcile.sh"
CSI_RECONCILE = REPO_ROOT / "stages/kustomize/csi-driver-host-path/gc/reconcile.sh"
CSI_RESTORE = REPO_ROOT / "stages/kustomize/csi-driver-host-path/gc/restore-csi-driver.sh"
CSI_CRONJOB = REPO_ROOT / "stages/kustomize/csi-driver-host-path/gc/cronjob.yaml"
CSI_PRUNE = REPO_ROOT / "stages/kustomize/csi-driver-host-path/gc/prune-node-images.sh"
MCP_MODULE = REPO_ROOT / "host/mcp/vicegerent_mcp.py"


def load_mcp_module():
    spec = importlib.util.spec_from_file_location("vicegerent_mcp_recovery", MCP_MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerConvergenceTests(unittest.TestCase):
    def test_failed_orphan_uninstall_returns_nonzero_and_names_residual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text("agents: []\n", encoding="utf-8")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                VALUES_FILE={values!s}
                yq() {{ :; }}
                helmc() {{
                  if [[ "$1" == list ]]; then printf 'orphan\n'; else return 1; fi
                }}
                confirm() {{ return 0; }}
                warn() {{ printf '%s\n' "$*" >&2; }}
                source {RECONCILE_LIB!s}
                reconcile_agents
                """
            )
            result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("orphan", result.stderr)
        self.assertIn("residual", result.stderr.lower())


    def test_declined_orphan_pruning_fails_and_reports_exact_residuals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text("agents: []\n", encoding="utf-8")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                VALUES_FILE={values!s}
                yq() {{ :; }}
                helmc() {{
                  if [[ "$1" == list ]]; then printf 'orphan-a\\norphan-b\\n'; else return 99; fi
                }}
                confirm() {{ return 1; }}
                warn() {{ printf '%s\\n' "$*" >&2; }}
                source {RECONCILE_LIB!s}
                reconcile_agents
                """
            )
            result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("residual releases: orphan-a orphan-b", result.stderr)

    def test_unreadable_deployed_release_list_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text("agents: []\n", encoding="utf-8")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                VALUES_FILE={values!s}
                yq() {{ :; }}
                helmc() {{ return 77; }}
                confirm() {{ return 0; }}
                warn() {{ printf '%s\\n' "$*" >&2; }}
                source {RECONCILE_LIB!s}
                reconcile_agents
                """
            )
            result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unable to list deployed agent releases", result.stderr)

    def test_successful_uninstall_that_leaves_release_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            values.write_text("agents: []\n", encoding="utf-8")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                VALUES_FILE={values!s}
                yq() {{ :; }}
                helmc() {{
                  if [[ "$1" == list ]]; then printf 'orphan\\n'; else return 0; fi
                }}
                confirm() {{ return 0; }}
                warn() {{ printf '%s\\n' "$*" >&2; }}
                source {RECONCILE_LIB!s}
                reconcile_agents
                """
            )
            result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("residual releases: orphan", result.stderr)

    def test_all_failed_uninstalls_are_attempted_and_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = Path(directory) / "values.yaml"
            log = Path(directory) / "uninstalls.log"
            values.write_text("agents: []\n", encoding="utf-8")
            harness = textwrap.dedent(
                f"""
                set -euo pipefail
                VALUES_FILE={values!s}
                yq() {{ :; }}
                helmc() {{
                  if [[ "$1" == list ]]; then
                    printf 'orphan-a\\norphan-b\\n'
                  else
                    printf '%s\\n' "$2" >> {log!s}
                    return 77
                  fi
                }}
                confirm() {{ return 0; }}
                warn() {{ printf '%s\\n' "$*" >&2; }}
                source {RECONCILE_LIB!s}
                reconcile_agents
                """
            )
            result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)

            attempted = log.read_text(encoding="utf-8").splitlines()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(attempted, ["orphan-a", "orphan-b"])
        self.assertIn("residual releases: orphan-a orphan-b", result.stderr)


class RcloneCredentialConvergenceTests(unittest.TestCase):
    def test_start_repairs_divergent_host_auth_key_from_kind_secret(self) -> None:
        module = load_mcp_module()
        cloud = b"[default]\naws_access_key_id=cluster-access\naws_secret_access_key=cluster-secret\n"
        encoded = base64.b64encode(cloud).decode("ascii")
        completed = subprocess.CompletedProcess([], 0, stdout=encoded, stderr="")
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            authkey.write_text("stale-access,stale-secret\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=completed) as run,
            ):
                module.ensure_rclone_material()

            self.assertTrue(run.called)
            self.assertEqual(authkey.read_text(encoding="utf-8"), "cluster-access,cluster-secret\n")
            self.assertEqual(authkey.stat().st_mode & 0o777, 0o600)

    def test_start_restores_missing_host_auth_key_from_kind_secret(self) -> None:
        module = load_mcp_module()
        cloud = b"[default]\naws_access_key_id=cluster-access\naws_secret_access_key=cluster-secret\n"
        completed = subprocess.CompletedProcess([], 0, stdout=base64.b64encode(cloud).decode("ascii"), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=completed),
            ):
                self.assertTrue(module.ensure_rclone_material())

            self.assertEqual(authkey.read_text(encoding="utf-8"), "cluster-access,cluster-secret\n")
            self.assertEqual(authkey.stat().st_mode & 0o777, 0o600)

    def test_start_keeps_existing_auth_key_when_atomic_replace_fails(self) -> None:
        module = load_mcp_module()
        cloud = b"[default]\naws_access_key_id=cluster-access\naws_secret_access_key=cluster-secret\n"
        completed = subprocess.CompletedProcess([], 0, stdout=base64.b64encode(cloud).decode("ascii"), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            authkey.write_text("old-access,old-secret\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=completed),
                patch.object(Path, "replace", side_effect=OSError("disk full")),
            ):
                self.assertIs(module.ensure_rclone_material(), False)

            self.assertEqual(authkey.read_text(encoding="utf-8"), "old-access,old-secret\n")
            self.assertFalse(any(path.name != "auth-key" for path in Path(directory).iterdir()))

    def test_unavailable_kind_context_preserves_valid_host_auth_key(self) -> None:
        module = load_mcp_module()
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            authkey.write_text("cached-access,cached-secret\n", encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value=None),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertTrue(module.ensure_rclone_material())

        self.assertIn("Kind context is unavailable; preserving", stderr.getvalue())
        self.assertNotIn("missing", stderr.getvalue())

    def test_unreachable_kind_preserves_valid_host_auth_key_but_missing_secret_fails_closed(self) -> None:
        module = load_mcp_module()
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            authkey.write_text("cached-access,cached-secret\n", encoding="utf-8")
            unreachable = subprocess.CompletedProcess([], 1, stdout="", stderr="Unable to connect to the server: connection refused")
            missing = subprocess.CompletedProcess([], 1, stdout="", stderr='Error from server (NotFound): secrets "velero-credentials" not found')
            unauthorized = subprocess.CompletedProcess([], 1, stdout="", stderr="the server has asked for the client to provide credentials")
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=unreachable),
            ):
                self.assertTrue(module.ensure_rclone_material())
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=missing),
            ):
                self.assertFalse(module.ensure_rclone_material())
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=unauthorized),
            ):
                self.assertFalse(module.ensure_rclone_material())

    def test_corrupted_host_auth_key_is_replaced_from_authoritative_secret(self) -> None:
        module = load_mcp_module()
        cloud = b"[default]\naws_access_key_id=cluster-access\naws_secret_access_key=cluster-secret\n"
        completed = subprocess.CompletedProcess([], 0, stdout=base64.b64encode(cloud).decode("ascii"), stderr="")
        with tempfile.TemporaryDirectory() as directory:
            authkey = Path(directory) / "auth-key"
            authkey.write_bytes(b"\xff\xfe")
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
                patch.object(module.subprocess, "run", return_value=completed),
            ):
                self.assertTrue(module.ensure_rclone_material())

            self.assertEqual(authkey.read_text(encoding="utf-8"), "cluster-access,cluster-secret\n")

    def test_unavailable_kind_context_rejects_invalid_host_auth_key(self) -> None:
        module = load_mcp_module()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "auth-key").write_text("not-an-auth-key\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RCLONE_S3_HOST_DIR": directory}, clear=False),
                patch.object(module, "resolve_kind_context", return_value=None),
            ):
                self.assertFalse(module.ensure_rclone_material())

    def test_start_rejects_rclone_before_starting_workloads(self) -> None:
        module = load_mcp_module()
        workloads = Mock()
        with (
            patch.object(module, "runtime_paths", return_value={}),
            patch.object(module, "load_servers_config", return_value={}),
            patch.object(module, "vmcp_port", return_value=9999),
            patch.object(module, "operator_vmcp_port", return_value=9998),
            patch.object(module, "default_listen", return_value="127.0.0.1:9997"),
            patch.object(module, "ensure_rclone_material", return_value=False),
            patch.object(module, "run_workloads", workloads),
        ):
            self.assertEqual(module.start_stack(skip_workloads=False), 1)

        workloads.assert_not_called()

    def test_start_fails_closed_when_authoritative_secret_is_malformed(self) -> None:
        module = load_mcp_module()
        completed = subprocess.CompletedProcess([], 0, stdout=base64.b64encode(b"[default]\n").decode("ascii"), stderr="")
        with (
            patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
            patch.object(module.subprocess, "run", return_value=completed),
        ):
            self.assertIs(module.ensure_rclone_material(), False)
    def test_start_fails_closed_when_authoritative_secret_is_not_base64(self) -> None:
        module = load_mcp_module()
        completed = subprocess.CompletedProcess([], 0, stdout="not-base64", stderr="")
        with (
            patch.object(module, "resolve_kind_context", return_value="kind-vicegerent"),
            patch.object(module.subprocess, "run", return_value=completed),
        ):
            self.assertIs(module.ensure_rclone_material(), False)


class CsiRecoveryTests(unittest.TestCase):
    POST_SCALE_DOWN_FAILURES = ("backup", "rewrite", "move", "delete")

    def test_every_post_scale_down_failure_restores_csi_driver(self) -> None:
        for failure in self.POST_SCALE_DOWN_FAILURES:
            with self.subTest(operation=failure), tempfile.TemporaryDirectory() as directory:
                data_dir = Path(directory)
                orphan_dir = data_dir / "orphan"
                orphan_dir.mkdir()
                (orphan_dir / "payload").write_text("x", encoding="utf-8")
                old_timestamp = time.time() - 120
                os.utime(orphan_dir, (old_timestamp, old_timestamp))
                (data_dir / "state.json").write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
                bin_dir = data_dir / "bin"
                bin_dir.mkdir()
                log = data_dir / "kubectl.log"
                kubectl = bin_dir / "kubectl"
                kubectl.write_text(textwrap.dedent(f"""\
                    #!/bin/sh
                    if [ "$1" = get ]; then exit 0; fi
                    printf '%s\\n' "$*" >> {log!s}
                    case "$*" in *'replicas":1'* ) exit 0 ;; esac
                    exit 0
                """), encoding="utf-8")
                kubectl.chmod(0o755)
                failing = bin_dir / failure
                if failure == "backup":
                    # Date keeps backup naming deterministic; cp is the injected failing operation.
                    failing = bin_dir / "cp"
                elif failure == "rewrite":
                    failing = bin_dir / "jq"
                    failing.write_text(textwrap.dedent("""\
                        #!/bin/sh
                        case " $* " in
                          *' .Snapshots[]?.Id // empty '*) exit 0 ;;
                          *' .Volumes[]?.VolID '*) printf 'orphan\\n'; exit 0 ;;
                          *' -R '*) printf '\"orphan\"\\n'; exit 0 ;;
                          *' -s '*) printf '[\"orphan\"]\\n'; exit 0 ;;
                        esac
                        exit 77
                    """), encoding="utf-8")
                    failing.chmod(0o755)
                    failing = None
                elif failure == "move":
                    failing = bin_dir / "mv"
                elif failure == "delete":
                    failing = bin_dir / "rm"
                if failing is not None:
                    failing.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
                    failing.chmod(0o755)
                environment = os.environ | {
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "DATA_DIR": str(data_dir),
                    "GRACE_MINUTES": "0",
                }
                result = subprocess.run(["sh", str(CSI_RECONCILE)], text=True, capture_output=True, env=environment)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn('"replicas":1', log.read_text(encoding="utf-8"))

    def test_partial_filesystem_cleanup_preserves_state_for_a_safe_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            orphan_dir = data_dir / "orphan"
            orphan_dir.mkdir()
            (orphan_dir / "payload").write_text("x", encoding="utf-8")
            old_timestamp = time.time() - 120
            os.utime(orphan_dir, (old_timestamp, old_timestamp))
            state_file = data_dir / "state.json"
            state_file.write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
            bin_dir = data_dir / "bin"
            bin_dir.mkdir()
            log = data_dir / "kubectl.log"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                if [ "$1" = get ]; then exit 0; fi
                printf '%s\\n' "$*" >> {log!s}
            """), encoding="utf-8")
            kubectl.chmod(0o755)
            failing_rm = bin_dir / "rm"
            failing_rm.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
            failing_rm.chmod(0o755)
            environment = os.environ | {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(data_dir),
                "GRACE_MINUTES": "0",
            }

            first = subprocess.run(["sh", str(CSI_RECONCILE)], text=True, capture_output=True, env=environment)

            self.assertNotEqual(first.returncode, 0)
            self.assertIn("orphan", state_file.read_text(encoding="utf-8"))
            failing_rm.unlink()
            second = subprocess.run(["sh", str(CSI_RECONCILE)], text=True, capture_output=True, env=environment)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertFalse(orphan_dir.exists())
            self.assertNotIn("orphan", state_file.read_text(encoding="utf-8"))
            self.assertGreaterEqual(log.read_text(encoding="utf-8").count('"replicas":1'), 2)

    def test_restore_api_retry_exhaustion_attempts_exactly_three_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            orphan_dir = data_dir / "orphan"
            orphan_dir.mkdir()
            old_timestamp = time.time() - 120
            os.utime(orphan_dir, (old_timestamp, old_timestamp))
            (data_dir / "state.json").write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
            bin_dir = data_dir / "bin"
            bin_dir.mkdir()
            log = data_dir / "kubectl.log"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                if [ "$1" = get ]; then exit 0; fi
                printf '%s\\n' "$*" >> {log!s}
                case "$*" in *'replicas":1'* ) exit 91 ;; esac
            """), encoding="utf-8")
            kubectl.chmod(0o755)
            environment = os.environ | {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(data_dir),
                "GRACE_MINUTES": "0",
            }

            result = subprocess.run(["sh", str(CSI_RECONCILE)], text=True, capture_output=True, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text(encoding="utf-8").count('"replicas":1'), 3)
            self.assertIn("remains scaled down", result.stdout)

    def test_retry_restores_csi_before_pruning_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            orphan_dir = data_dir / "orphan"
            orphan_dir.mkdir()
            (orphan_dir / "payload").write_text("x", encoding="utf-8")
            old_timestamp = time.time() - 120
            os.utime(orphan_dir, (old_timestamp, old_timestamp))
            state_file = data_dir / "state.json"
            state_file.write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
            bin_dir = data_dir / "bin"
            bin_dir.mkdir()
            restored = data_dir / "driver-restored"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                case "$*" in
                  *'replicas":1'*)
                    [ "${{RESTORE_MODE:-retry}}" = fail ] && exit 91
                    touch {restored!s}
                    ;;
                esac
                if [ "$1" = wait ] && [ ! -f {restored!s} ]; then exit 91; fi
            """), encoding="utf-8")
            kubectl.chmod(0o755)
            crictl = bin_dir / "crictl"
            crictl.write_text(
                f"#!/bin/sh\n[ -f {restored!s} ] || exit 92\nprintf 'Deleted: stale-image\\n'\n",
                encoding="utf-8",
            )
            crictl.chmod(0o755)
            base_environment = os.environ | {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(data_dir),
                "GRACE_MINUTES": "0",
                "CRICTL": str(crictl),
            }

            failed_first_attempt = subprocess.run(
                ["sh", str(CSI_RECONCILE)], text=True, capture_output=True,
                env=base_environment | {"RESTORE_MODE": "fail"},
            )
            restore = subprocess.run(["sh", str(CSI_RESTORE)], text=True, capture_output=True, env=base_environment)
            prune = subprocess.run(["sh", str(CSI_PRUNE)], text=True, capture_output=True, env=base_environment)

            self.assertNotEqual(failed_first_attempt.returncode, 0)
            self.assertFalse(orphan_dir.exists())
            self.assertNotIn("orphan", state_file.read_text(encoding="utf-8"))
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertTrue(restored.exists())
            self.assertEqual(prune.returncode, 0, prune.stderr)
            self.assertIn("pruned 1 unused node image", prune.stdout)
            manifest = CSI_CRONJOB.read_text(encoding="utf-8")
            self.assertLess(manifest.index("name: restore-csi-driver"), manifest.index("name: prune-node-images"))

    def test_post_scale_down_api_failure_still_restores_the_driver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            orphan_dir = data_dir / "orphan"
            orphan_dir.mkdir()
            old_timestamp = time.time() - 120
            os.utime(orphan_dir, (old_timestamp, old_timestamp))
            (data_dir / "state.json").write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
            bin_dir = data_dir / "bin"
            bin_dir.mkdir()
            log = data_dir / "kubectl.log"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                if [ "$1" = get ]; then exit 0; fi
                printf '%s\\n' "$*" >> {log!s}
                case "$*" in *'--for=delete'*) exit 75 ;; esac
            """), encoding="utf-8")
            kubectl.chmod(0o755)
            environment = os.environ | {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(data_dir),
                "GRACE_MINUTES": "0",
            }

            result = subprocess.run(["sh", str(CSI_RECONCILE)], text=True, capture_output=True, env=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"replicas":1', log.read_text(encoding="utf-8"))

    def test_signal_during_post_scale_down_filesystem_work_restores_the_driver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            orphan_dir = data_dir / "orphan"
            orphan_dir.mkdir()
            old_timestamp = time.time() - 120
            os.utime(orphan_dir, (old_timestamp, old_timestamp))
            (data_dir / "state.json").write_text('{"Volumes":[{"VolID":"orphan"}],"Snapshots":[]}\n', encoding="utf-8")
            bin_dir = data_dir / "bin"
            bin_dir.mkdir()
            log = data_dir / "kubectl.log"
            ready = data_dir / "backup-started"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(textwrap.dedent(f"""\
                #!/bin/sh
                if [ "$1" = get ]; then exit 0; fi
                printf '%s\\n' "$*" >> {log!s}
            """), encoding="utf-8")
            kubectl.chmod(0o755)
            cp = bin_dir / "cp"
            cp.write_text(f"#!/bin/sh\ntouch {ready!s}\nsleep 30\n", encoding="utf-8")
            cp.chmod(0o755)
            environment = os.environ | {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DATA_DIR": str(data_dir),
                "GRACE_MINUTES": "0",
            }
            process = subprocess.Popen(
                ["sh", str(CSI_RECONCILE)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "reconcile did not reach the post-scale-down backup")
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 143, stderr)
            self.assertIn('"replicas":1', log.read_text(encoding="utf-8"))
            self.assertIn("back up", stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
