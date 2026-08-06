#!/usr/bin/env python3
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("reconcile.py")
spec = importlib.util.spec_from_file_location("host_brew_reconcile", MODULE_PATH)
assert spec is not None and spec.loader is not None
reconcile = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reconcile
spec.loader.exec_module(reconcile)


class FakeBrew:
    def __init__(self, responses, *, pinned=()):
        self.responses = responses
        self.commands = []
        self.pinned = set(pinned)

    def run(self, *args, check=False):
        self.commands.append(args)
        if args[0] == "unpin":
            self.pinned.discard(args[1])
        response = self.responses.get(args, (0, "", ""))
        if args[0] == "uninstall" and args[-1] in self.pinned:
            response = (1, "", f"{args[-1]} is pinned")
        stdout = None if check else response[1]
        stderr = None if check else response[2]
        result = subprocess.CompletedProcess(args, response[0], stdout, stderr)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
        return result


class ManifestTests(unittest.TestCase):
    def test_terminal_notifier_is_required_and_uses_homebrew_core(self):
        manifest = reconcile.load_manifest()
        package = next(package for package in manifest.packages if package.name == "terminal-notifier")

        self.assertTrue(package.required)
        self.assertEqual(package.formula, "homebrew/core/terminal-notifier")
        self.assertEqual(
            package.replaces,
            ("vicegerent/packages/terminal-notifier@2.0.0",),
        )
        self.assertTrue(getattr(package, "force_bottle", False))
        self.assertFalse(hasattr(package, "xcode_first_launch"))

    def test_manifest_requires_exact_version_in_formula_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "tap": {"name": "vicegerent/packages", "url": "git@example/repo.git"},
                "packages": [{
                    "name": "toolhive",
                    "formula": "vicegerent/packages/thv",
                    "version": "0.42.0",
                    "binary": "thv",
                    "versionArgs": ["version"],
                    "replaces": ["stacklok/tap/thv"]
                }]
            }))

            with self.assertRaisesRegex(ValueError, "version-qualified"):
                reconcile.load_manifest(path)

    def test_manifest_loads_version_qualified_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "tap": {"name": "vicegerent/packages", "url": "git@example/repo.git"},
                "packages": [{
                    "name": "toolhive",
                    "formula": "vicegerent/packages/thv@0.42.0",
                    "version": "0.42.0",
                    "binary": "thv",
                    "versionArgs": ["version"],
                    "replaces": ["stacklok/tap/thv"]
                }]
            }))

            manifest = reconcile.load_manifest(path)

            self.assertEqual(manifest.tap.name, "vicegerent/packages")
            self.assertEqual(manifest.packages[0].short_formula, "thv@0.42.0")

    def test_manifest_rejects_unsupported_formula_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "tap": {"name": "vicegerent/packages", "url": "git@example/repo.git"},
                "packages": [{
                    "name": "terminal-notifier",
                    "formula": "untrusted/packages/terminal-notifier",
                    "version": "2.0.0",
                    "binary": "terminal-notifier",
                    "versionArgs": ["-help"],
                }]
            }))

            with self.assertRaisesRegex(ValueError, "unsupported formula source"):
                reconcile.load_manifest(path)


class FormulaValidationTests(unittest.TestCase):
    def test_homebrew_core_formula_does_not_require_a_repository_formula(self):
        package = reconcile.Package(
            name="terminal-notifier",
            formula="homebrew/core/terminal-notifier",
            version="2.0.0",
            binary="terminal-notifier",
            version_args=("-help",),
            replaces=(),
        )
        manifest = reconcile.Manifest(
            tap=reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
            packages=(package,),
        )
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = reconcile.validate_formulae(manifest, Path(tmp))

        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_rejects_go_ldflags_with_x_value_joined_to_flag(self):
        package = reconcile.Package(
            name="rclone",
            formula="vicegerent/packages/rclone@1.75.0",
            version="1.75.0",
            binary="rclone",
            version_args=("version",),
            replaces=("rclone",),
        )
        manifest = reconcile.Manifest(
            tap=reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
            packages=(package,),
        )
        with tempfile.TemporaryDirectory() as tmp:
            formula_dir = Path(tmp) / "Formula"
            formula_dir.mkdir()
            (formula_dir / "rclone@1.75.0.rb").write_text("""\
class RcloneAT1750 < Formula
  version "1.75.0"
  sha256 "abc"
  keg_only :versioned_formula
  ldflags = %W[-Xgithub.com/rclone/rclone/fs.Version=v#{version}]
end
""")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = reconcile.validate_formulae(manifest, Path(tmp))

            self.assertEqual(result, 1)
            self.assertIn("Go -X linker flag requires a separate value", stderr.getvalue())


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.package = reconcile.Package(
            name="toolhive",
            formula="vicegerent/packages/thv@0.42.0",
            version="0.42.0",
            binary="thv",
            version_args=("version",),
            replaces=("stacklok/tap/thv",),
        )
        self.prefix = "/opt/homebrew"
        self.formula_prefix = "/opt/homebrew/Cellar/thv@0.42.0/0.42.0"

    def brew(self):
        return FakeBrew({
            ("--prefix",): (0, self.prefix + "\n", ""),
            ("list", "--pinned"): (0, "thv@0.42.0\n", ""),
        })

    def resolve_desired(self, value):
        if value.endswith("/bin/thv"):
            return self.formula_prefix + "/bin/thv"
        return self.formula_prefix

    def test_status_requires_formula_version_binary_ownership_and_pin(self):
        status = reconcile.package_status(
            self.package,
            self.brew(),
            which=lambda _: self.prefix + "/bin/thv",
            exists=lambda _: True,
            resolve=self.resolve_desired,
            probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.42.0\n", ""),
        )

        self.assertTrue(status.ok)
        self.assertEqual(status.observed_version, "0.42.0")

    def test_status_rejects_an_installed_replacement(self):
        brew = self.brew()
        brew.responses[("list", "--versions", "stacklok/tap/thv")] = (
            0, "thv 0.43.0\n", "",
        )

        status = reconcile.package_status(
            self.package,
            brew,
            which=lambda _: self.prefix + "/bin/thv",
            exists=lambda _: True,
            resolve=self.resolve_desired,
            probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.42.0\n", ""),
        )

        self.assertFalse(status.ok)
        self.assertIn("replacement is still installed", status.detail)

    def test_status_rejects_binary_owned_by_floating_formula(self):
        status = reconcile.package_status(
            self.package,
            self.brew(),
            which=lambda _: "/opt/homebrew/bin/thv",
            exists=lambda _: True,
            resolve=lambda value: (
                "/opt/homebrew/Cellar/thv/0.43.0/bin/thv"
                if value.endswith("/bin/thv") else self.formula_prefix
            ),
            probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.42.0\n", ""),
        )

        self.assertFalse(status.ok)
        self.assertIn("not owned", status.detail)

    def test_status_reports_the_observed_wrong_version(self):
        status = reconcile.package_status(
            self.package,
            self.brew(),
            which=lambda _: self.prefix + "/bin/thv",
            exists=lambda _: True,
            resolve=self.resolve_desired,
            probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.43.0\n", ""),
        )

        self.assertFalse(status.ok)
        self.assertEqual(status.observed_version, "0.43.0")
        self.assertIn("expected 0.42.0, found 0.43.0", status.detail)

    def test_optional_package_drift_does_not_fail_check(self):
        package = reconcile.Package(
            name="terminal-notifier",
            formula="vicegerent/packages/terminal-notifier@2.0.0",
            version="2.0.0",
            binary="terminal-notifier",
            version_args=("-help",),
            replaces=("terminal-notifier",),
            required=False,
        )
        manifest = reconcile.Manifest(
            tap=reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
            packages=(package,),
        )
        brew = FakeBrew({("--prefix",): (0, "/opt/homebrew\n", "")})

        self.assertEqual(reconcile.check(manifest, brew), 0)


class TapTests(unittest.TestCase):
    def test_missing_tap_is_added_with_pinned_remote(self):
        brew = FakeBrew({("tap",): (0, "homebrew/core\n", "")})
        tap = reconcile.Tap("vicegerent/packages", "git@example/repo.git")

        reconcile.ensure_tap(tap, brew)

        self.assertEqual(brew.commands, [
            ("tap",),
            ("tap", "vicegerent/packages", "git@example/repo.git"),
        ])

    def test_existing_tap_is_not_readded(self):
        brew = FakeBrew({("tap",): (0, "vicegerent/packages\n", "")})
        tap = reconcile.Tap("vicegerent/packages", "git@example/repo.git")

        reconcile.ensure_tap(tap, brew)

        self.assertEqual(brew.commands, [("tap",)])

    def test_tap_is_synchronized_to_the_invoking_checkout(self):
        brew = FakeBrew({
            ("--repo", "vicegerent/packages"): (0, "/opt/homebrew/tap\n", ""),
        })
        commands = []

        def run_git(command):
            commands.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        reconcile.sync_tap(
            reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
            brew,
            Path("/workspace/vicegerent-agents"),
            run_git=run_git,
        )

        self.assertEqual(commands, [
            ("git", "-C", "/opt/homebrew/tap", "fetch", "--force", "/workspace/vicegerent-agents", "HEAD"),
            ("git", "-C", "/opt/homebrew/tap", "checkout", "--detach", "--force", "FETCH_HEAD"),
        ])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.package = reconcile.Package(
            name="toolhive",
            formula="vicegerent/packages/thv@0.42.0",
            version="0.42.0",
            binary="thv",
            version_args=("version",),
            replaces=("stacklok/tap/thv",),
        )

    def test_reconcile_installs_verifies_switches_and_removes_floating_formula(self):
        prefix = "/opt/homebrew/opt/thv@0.42.0"
        brew = FakeBrew({
            ("list", "--versions", self.package.formula): (1, "", "missing"),
            ("--prefix", self.package.formula): (0, prefix + "\n", ""),
            ("list", "--versions", "stacklok/tap/thv"): (0, "thv 0.43.0\n", ""),
        })

        reconcile.reconcile_package(
            self.package,
            brew,
            probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.42.0\n", ""),
        )

        self.assertEqual(brew.commands, [
            ("list", "--versions", self.package.formula),
            ("install", self.package.formula),
            ("--prefix", self.package.formula),
            ("list", "--versions", "stacklok/tap/thv"),
            ("unlink", "stacklok/tap/thv"),
            ("link", "--force", self.package.formula),
            ("pin", self.package.formula),
            ("list", "--pinned"),
            ("uninstall", "--ignore-dependencies", "stacklok/tap/thv"),
        ])

    def test_reconcile_installs_homebrew_core_terminal_notifier_without_xcode(self):
        manifest = reconcile.load_manifest()
        package = next(package for package in manifest.packages if package.name == "terminal-notifier")
        prefix = "/opt/homebrew/opt/terminal-notifier"
        replacement = "vicegerent/packages/terminal-notifier@2.0.0"
        brew = FakeBrew({
            ("list", "--versions", package.formula): (1, "", "missing"),
            ("--prefix", package.formula): (0, prefix + "\n", ""),
            (
                "list", "--versions", replacement,
            ): (0, "terminal-notifier@2.0.0 2.0.0\n", ""),
            ("list", "--pinned"): (0, "terminal-notifier@2.0.0\n", ""),
        }, pinned=(replacement,))

        reconcile.reconcile_package(
            package,
            brew,
            probe=lambda _: subprocess.CompletedProcess([], 0, "terminal-notifier 2.0.0\n", ""),
        )

        self.assertEqual(brew.commands, [
            ("list", "--versions", "homebrew/core/terminal-notifier"),
            ("install", "--force-bottle", "homebrew/core/terminal-notifier"),
            ("--prefix", "homebrew/core/terminal-notifier"),
            ("list", "--versions", replacement),
            ("unlink", replacement),
            ("link", "--force", "homebrew/core/terminal-notifier"),
            ("pin", "homebrew/core/terminal-notifier"),
            ("list", "--pinned"),
            ("unpin", replacement),
            ("uninstall", "--ignore-dependencies", replacement),
        ])

    def test_reconcile_rejects_wrong_installed_homebrew_core_version(self):
        manifest = reconcile.load_manifest()
        package = next(package for package in manifest.packages if package.name == "terminal-notifier")
        prefix = "/opt/homebrew/opt/terminal-notifier"
        brew = FakeBrew({
            (
                "list", "--versions", "homebrew/core/terminal-notifier",
            ): (0, "terminal-notifier 2.1.0\n", ""),
            ("--prefix", "homebrew/core/terminal-notifier"): (0, prefix + "\n", ""),
        })

        with self.assertRaisesRegex(reconcile.ReconcileError, "expected 2.0.0"):
            reconcile.reconcile_package(
                package,
                brew,
                probe=lambda _: subprocess.CompletedProcess(
                    [], 0, "terminal-notifier 2.1.0\n", "",
                ),
            )

        self.assertEqual(brew.commands, [
            ("list", "--versions", "homebrew/core/terminal-notifier"),
            ("--prefix", "homebrew/core/terminal-notifier"),
        ])

    def test_reconcile_does_not_unlink_working_formula_when_new_probe_fails(self):
        prefix = "/opt/homebrew/opt/thv@0.42.0"
        brew = FakeBrew({
            ("list", "--versions", self.package.formula): (1, "", "missing"),
            ("--prefix", self.package.formula): (0, prefix + "\n", ""),
        })

        with self.assertRaisesRegex(reconcile.ReconcileError, "version probe"):
            reconcile.reconcile_package(
                self.package,
                brew,
                probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.43.0\n", ""),
            )

        self.assertNotIn(("unlink", "stacklok/tap/thv"), brew.commands)
        self.assertFalse(any(command[0] == "uninstall" for command in brew.commands))

    def test_failed_link_is_fatal_when_rollback_cannot_restore_previous_formula(self):
        prefix = "/opt/homebrew/opt/thv@0.42.0"
        brew = FakeBrew({
            ("list", "--versions", self.package.formula): (0, "thv@0.42.0 0.42.0\n", ""),
            ("--prefix", self.package.formula): (0, prefix + "\n", ""),
            ("list", "--versions", "stacklok/tap/thv"): (0, "thv 0.43.0\n", ""),
            ("link", "--force", self.package.formula): (1, "", "conflict"),
            ("link", "--force", "stacklok/tap/thv"): (1, "", "still conflicted"),
        })

        with self.assertRaisesRegex(reconcile.MigrationError, "rollback failed"):
            reconcile.reconcile_package(
                self.package,
                brew,
                probe=lambda _: subprocess.CompletedProcess([], 0, "ToolHive v0.42.0\n", ""),
            )

        self.assertIn(("link", "--force", "stacklok/tap/thv"), brew.commands)


if __name__ == "__main__":
    unittest.main()
