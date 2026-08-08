#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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


def native_notifier():
    return reconcile.Notifier(
        version="1.0.0",
        bundle_identifier="com.hahomelabs.vicegerent.notifier",
        obsolete_formulae=(
            "homebrew/core/terminal-notifier",
            "vicegerent/packages/terminal-notifier@2.0.0",
        ),
    )


def manifest_with(*packages):
    return reconcile.Manifest(
        tap=reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
        packages=packages,
        notifier=native_notifier(),
    )


def manifest_json(packages):
    return {
        "schemaVersion": 1,
        "tap": {"name": "vicegerent/packages", "url": "git@example/repo.git"},
        "notifier": {
            "version": "1.0.0",
            "bundleIdentifier": "com.hahomelabs.vicegerent.notifier",
            "obsoleteFormulae": ["homebrew/core/terminal-notifier"],
        },
        "packages": packages,
    }


def write_notifier_sources(root):
    notifier_dir = root / "host" / "notifier"
    notifier_dir.mkdir(parents=True)
    (notifier_dir / "Info.plist").write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.hahomelabs.vicegerent.notifier</string>
<key>CFBundleShortVersionString</key><string>1.0.0</string>
<key>CFBundleIconFile</key><string>Vicegerent.icns</string>
</dict></plist>
"""
    )
    (notifier_dir / "main.m").write_text('static NSString *version = @"1.0.0";\n')
    (notifier_dir / "Vicegerent.icns").write_bytes(b"icon")


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class DiagnosticTests(unittest.TestCase):
    def test_notification_authorization_warning_is_yellow_on_a_terminal(self):
        stderr = TTYBuffer()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "stderr", stderr),
        ):
            reconcile._print_diagnostic("WARNING:", "authorization denied", "1;33")

        self.assertEqual(
            stderr.getvalue(),
            "\033[1;33mWARNING:\033[0m authorization denied\n",
        )

    def test_diagnostic_honors_no_color(self):
        stderr = TTYBuffer()
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            patch.object(sys, "stderr", stderr),
        ):
            reconcile._print_diagnostic("WARNING:", "authorization denied", "1;33")

        self.assertEqual(stderr.getvalue(), "WARNING: authorization denied\n")


class ManifestTests(unittest.TestCase):
    def test_native_notifier_replaces_both_terminal_notifier_formulae(self):
        manifest = reconcile.load_manifest()
        self.assertFalse(any(package.name == "terminal-notifier" for package in manifest.packages))
        self.assertEqual(manifest.notifier.version, "1.0.0")
        self.assertEqual(
            manifest.notifier.bundle_identifier,
            "com.hahomelabs.vicegerent.notifier",
        )
        self.assertEqual(
            manifest.notifier.obsolete_formulae,
            (
                "homebrew/core/terminal-notifier",
                "vicegerent/packages/terminal-notifier@2.0.0",
            ),
        )

    def test_manifest_requires_exact_version_in_formula_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps(manifest_json([{
                    "name": "toolhive",
                    "formula": "vicegerent/packages/thv",
                    "version": "0.42.0",
                    "binary": "thv",
                    "versionArgs": ["version"],
                    "replaces": ["stacklok/tap/thv"]
                }])))

            with self.assertRaisesRegex(ValueError, "version-qualified"):
                reconcile.load_manifest(path)

    def test_manifest_loads_version_qualified_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps(manifest_json([{
                    "name": "toolhive",
                    "formula": "vicegerent/packages/thv@0.42.0",
                    "version": "0.42.0",
                    "binary": "thv",
                    "versionArgs": ["version"],
                    "replaces": ["stacklok/tap/thv"]
                }])))

            manifest = reconcile.load_manifest(path)

            self.assertEqual(manifest.tap.name, "vicegerent/packages")
            self.assertEqual(manifest.packages[0].short_formula, "thv@0.42.0")

    def test_manifest_rejects_unsupported_formula_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.json"
            path.write_text(json.dumps(manifest_json([{
                "name": "toolhive",
                "formula": "untrusted/packages/thv@0.42.0",
                "version": "0.42.0",
                "binary": "thv",
                "versionArgs": ["version"],
            }])))

            with self.assertRaisesRegex(ValueError, "unsupported formula source"):
                reconcile.load_manifest(path)

    def test_manifest_requires_native_notifier_configuration(self):
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
                }]
            }))

            with self.assertRaises(KeyError):
                reconcile.load_manifest(path)


class FormulaValidationTests(unittest.TestCase):
    def test_native_notifier_sources_match_manifest(self):
        package = reconcile.Package(
            name="toolhive",
            formula="vicegerent/packages/thv@0.42.0",
            version="0.42.0",
            binary="thv",
            version_args=("version",),
            replaces=(),
        )
        manifest = manifest_with(package)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            formula_dir = root / "Formula"
            formula_dir.mkdir()
            (formula_dir / "thv@0.42.0.rb").write_text("""\
class ThvAT0420 < Formula
  version "0.42.0"
  sha256 "abc"
  keg_only :versioned_formula
end
""")
            write_notifier_sources(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = reconcile.validate_formulae(manifest, root)

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
        manifest = manifest_with(package)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            formula_dir = root / "Formula"
            formula_dir.mkdir()
            (formula_dir / "rclone@1.75.0.rb").write_text("""\
class RcloneAT1750 < Formula
  version "1.75.0"
  sha256 "abc"
  keg_only :versioned_formula
  ldflags = %W[-Xgithub.com/rclone/rclone/fs.Version=v#{version}]
end
""")
            write_notifier_sources(root)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = reconcile.validate_formulae(manifest, root)

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

    def test_status_finds_an_orphaned_replacement_by_short_name(self):
        brew = self.brew()
        brew.responses[("list", "--versions", "stacklok/tap/thv")] = (
            1, "", "No available formula",
        )
        brew.responses[("list", "--versions", "thv")] = (
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
        self.assertIn("replacement is still installed: thv", status.detail)

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
            name="toolhive",
            formula="vicegerent/packages/thv@0.42.0",
            version="0.42.0",
            binary="thv",
            version_args=("version",),
            replaces=(),
            required=False,
        )
        manifest = manifest_with(package)
        brew = FakeBrew({("--prefix",): (0, "/opt/homebrew\n", "")})

        self.assertEqual(
            reconcile.check(
                manifest,
                brew,
                check_notifier=lambda *_: reconcile.NotifierStatus(True, "authorized"),
            ),
            0,
        )


class ConcurrentStatusTests(unittest.TestCase):
    def test_independent_status_probes_run_concurrently(self):
        packages = tuple(
            reconcile.Package(
                name=name,
                formula=f"vicegerent/packages/{name}@1.0.0",
                version="1.0.0",
                binary=name,
                version_args=("--version",),
                replaces=(),
            )
            for name in ("first", "second")
        )
        manifest = reconcile.Manifest(
            tap=reconcile.Tap("vicegerent/packages", "git@example/repo.git"),
            packages=packages,
            notifier=reconcile.Notifier(
                version="1.0.0",
                bundle_identifier="com.hahomelabs.vicegerent.notifier",
                obsolete_formulae=(),
            ),
        )
        barrier = threading.Barrier(3)

        def package_probe(package, _brew):
            barrier.wait(timeout=5)
            return reconcile.PackageStatus(package, True, package.version, "current")

        def notifier_probe(notifier, _repo_root):
            barrier.wait(timeout=5)
            return reconcile.NotifierStatus(True, f"{notifier.version} current")

        with patch.object(reconcile, "package_status", side_effect=package_probe):
            statuses, notifier, obsolete = reconcile.inspect_host_status(
                manifest,
                FakeBrew({}),
                Path("/repo"),
                check_notifier=notifier_probe,
            )

        self.assertEqual([status.package.name for status in statuses], ["first", "second"])
        self.assertTrue(notifier.ok)
        self.assertEqual(obsolete, [])


class BundleSignatureTests(unittest.TestCase):
    def setUp(self):
        self.bundle = "/Users/test/.vicegerent/notifier/Vicegerent Notifier.app"

    def test_verifier_uses_deep_strict_codesign_validation(self):
        calls = []

        def run(command, **kwargs):
            calls.append((tuple(command), kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        self.assertTrue(reconcile.verify_app_bundle_signature(self.bundle, run=run))
        self.assertEqual(calls[0][0], (
            "codesign", "--verify", "--deep", "--strict", self.bundle,
        ))
        self.assertFalse(calls[0][1]["check"])

    def test_signer_uses_an_ad_hoc_deep_signature(self):
        calls = []

        def run(command, **kwargs):
            calls.append((tuple(command), kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        reconcile.sign_app_bundle(
            self.bundle,
            bundle_identifier="com.hahomelabs.vicegerent.notifier",
            run=run,
        )

        self.assertEqual(calls[0][0], (
            "codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            "-r",
            '=designated => identifier "com.hahomelabs.vicegerent.notifier"',
            self.bundle,
        ))
        self.assertTrue(calls[0][1]["check"])

    def test_requirement_verifier_requires_the_stable_bundle_identifier(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                'designated => identifier "com.hahomelabs.vicegerent.notifier"\n',
            )

        self.assertTrue(
            reconcile.verify_app_bundle_requirement(
                self.bundle,
                "com.hahomelabs.vicegerent.notifier",
                run=run,
            )
        )


class NativeNotifierTests(unittest.TestCase):
    def _installed_notifier(self, root):
        write_notifier_sources(root)
        install_root = root / "installed"
        paths = reconcile.notifier_paths(install_root)
        paths["binary"].parent.mkdir(parents=True)
        paths["digest"].parent.mkdir(parents=True, exist_ok=True)
        paths["binary"].write_text("binary")
        paths["digest"].write_text(reconcile.notifier_source_digest(root) + "\n")
        return install_root

    def test_status_verifies_source_signature_version_and_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_root = self._installed_notifier(root)

            def probe(command):
                if command[-1] == "version":
                    return subprocess.CompletedProcess(command, 0, "vicegerent-notifier 1.0.0\n", "")
                return subprocess.CompletedProcess(command, 0, "authorized\n", "")

            status = reconcile.notifier_status(
                native_notifier(),
                root,
                install_root=install_root,
                verify_signature=lambda _: True,
                verify_requirement=lambda *_: True,
                probe=probe,
            )

        self.assertTrue(status.ok)
        self.assertEqual(status.detail, "signed, registered, and authorized")

    def test_status_reports_notification_authorization_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_root = self._installed_notifier(root)

            def probe(command):
                if command[-1] == "version":
                    return subprocess.CompletedProcess(command, 0, "vicegerent-notifier 1.0.0\n", "")
                return subprocess.CompletedProcess(command, 4, "denied\n", "")

            status = reconcile.notifier_status(
                native_notifier(),
                root,
                install_root=install_root,
                verify_signature=lambda _: True,
                verify_requirement=lambda *_: True,
                probe=probe,
            )

        self.assertFalse(status.ok)
        self.assertEqual(status.detail, "denied")

    def test_reconcile_builds_signs_registers_and_authorizes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            write_notifier_sources(root)
            install_root = Path(tmp) / "home" / ".vicegerent" / "notifier"

            def run(command, **kwargs):
                calls.append(tuple(str(part) for part in command))
                if command[:2] == ["xcrun", "clang"]:
                    Path(command[command.index("-o") + 1]).write_text("binary")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[-1] == "version":
                    return subprocess.CompletedProcess(command, 0, "vicegerent-notifier 1.0.0\n", "")
                if command[-1] == "authorize":
                    return subprocess.CompletedProcess(command, 0, "authorized\n", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with redirect_stdout(io.StringIO()):
                reconcile.reconcile_notifier(
                    native_notifier(),
                    root,
                    install_root=install_root,
                    run=run,
                    verify_signature=lambda _: True,
                    verify_requirement=lambda *_: True,
                )

            paths = reconcile.notifier_paths(install_root)
            self.assertTrue(paths["binary"].is_file())
            self.assertTrue(
                (paths["bundle"] / "Contents" / "Resources" / "Vicegerent.icns").is_file()
            )
            self.assertEqual(
                paths["digest"].read_text().strip(),
                reconcile.notifier_source_digest(root),
            )

        self.assertTrue(any(command[:2] == ("xcrun", "clang") for command in calls))
        self.assertTrue(any(command[:2] == ("codesign", "--force") for command in calls))
        self.assertTrue(any(command[0] == str(reconcile.LSREGISTER) for command in calls))
        self.assertTrue(any(command[-1] == "authorize" for command in calls))
        self.assertTrue(any(command[-1] == "remove-all" for command in calls))

    def test_obsolete_formula_cleanup_uses_orphaned_short_name(self):
        core = "homebrew/core/terminal-notifier"
        custom = "vicegerent/packages/terminal-notifier@2.0.0"
        brew = FakeBrew({
            ("list", "--versions", core): (0, "terminal-notifier 2.0.0\n", ""),
            ("list", "--versions", custom): (1, "", "No available formula"),
            ("list", "--versions", "terminal-notifier@2.0.0"): (
                0,
                "terminal-notifier@2.0.0 2.0.0\n",
                "",
            ),
            ("list", "--pinned"): (0, "terminal-notifier terminal-notifier@2.0.0\n", ""),
        }, pinned=("terminal-notifier", "terminal-notifier@2.0.0"))

        reconcile.remove_obsolete_formulae(native_notifier(), brew)

        self.assertIn(("unpin", core), brew.commands)
        self.assertIn(("uninstall", "--ignore-dependencies", core), brew.commands)
        self.assertIn(("unpin", "terminal-notifier@2.0.0"), brew.commands)
        self.assertIn(
            ("uninstall", "--ignore-dependencies", "terminal-notifier@2.0.0"),
            brew.commands,
        )


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

    def test_apply_skips_tap_and_reconciliation_when_everything_is_current(self):
        status = reconcile.PackageStatus(
            self.package,
            True,
            self.package.version,
            "exact version installed, linked, and pinned",
        )
        brew = FakeBrew({})
        stdout = io.StringIO()
        with (
            patch.object(reconcile, "package_status", return_value=status),
            patch.object(reconcile, "ensure_tap") as ensure_tap,
            patch.object(reconcile, "sync_tap") as sync_tap,
            patch.object(reconcile, "reconcile_package") as reconcile_package,
            patch.object(
                reconcile,
                "notifier_status",
                return_value=reconcile.NotifierStatus(True, "authorized"),
            ),
            patch.object(reconcile, "reconcile_notifier") as reconcile_notifier,
            patch.object(reconcile, "check", return_value=0),
            redirect_stdout(stdout),
        ):
            result = reconcile.apply(manifest_with(self.package), brew, Path("/repo"))

        self.assertEqual(result, 0)
        self.assertIn("vicegerent-notifier 1.0.0 is already reconciled.", stdout.getvalue())
        ensure_tap.assert_not_called()
        sync_tap.assert_not_called()
        reconcile_package.assert_not_called()
        reconcile_notifier.assert_not_called()

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
            ("install", "--overwrite", self.package.formula),
            ("--prefix", self.package.formula),
            ("list", "--versions", "stacklok/tap/thv"),
            ("unlink", "stacklok/tap/thv"),
            ("link", "--force", self.package.formula),
            ("pin", self.package.formula),
            ("list", "--pinned"),
            ("uninstall", "--ignore-dependencies", "stacklok/tap/thv"),
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
