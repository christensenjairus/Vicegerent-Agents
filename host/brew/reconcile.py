#!/usr/bin/env python3
"""Reconcile the exact Homebrew formula versions required by the host stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).with_name("packages.json")
NOTIFIER_SOURCE_FILES = (
    Path("host/notifier/main.m"),
    Path("host/notifier/Info.plist"),
    Path("host/notifier/Vicegerent.icns"),
)
DEFAULT_NOTIFIER_INSTALL_ROOT = Path.home() / ".vicegerent" / "notifier"
NOTIFIER_BUNDLE_NAME = "Vicegerent Notifier.app"
NOTIFIER_BINARY_NAME = "vicegerent-notifier"
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


@dataclass(frozen=True)
class Tap:
    name: str
    url: str


@dataclass(frozen=True)
class Package:
    name: str
    formula: str
    version: str
    binary: str
    version_args: tuple[str, ...]
    replaces: tuple[str, ...]
    required: bool = True

    @property
    def short_formula(self) -> str:
        return self.formula.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Notifier:
    version: str
    bundle_identifier: str


@dataclass(frozen=True)
class Manifest:
    tap: Tap
    packages: tuple[Package, ...]
    notifier: Notifier


@dataclass(frozen=True)
class NotifierStatus:
    ok: bool
    detail: str


@dataclass(frozen=True)
class PackageStatus:
    package: Package
    ok: bool
    observed_version: str | None
    detail: str


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    data = json.loads(path.read_text())
    if data.get("schemaVersion") != 1:
        raise ValueError("unsupported host package manifest schemaVersion")
    tap_data = data.get("tap", {})
    tap = Tap(name=tap_data["name"], url=tap_data["url"])
    notifier_data = data.get("notifier", {})
    bundle_identifier = notifier_data["bundleIdentifier"]
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_identifier):
        raise ValueError(f"invalid notifier bundle identifier: {bundle_identifier}")
    notifier = Notifier(
        version=notifier_data["version"],
        bundle_identifier=bundle_identifier,
    )
    packages: list[Package] = []
    for raw in data.get("packages", []):
        formula = raw["formula"]
        version = raw["version"]
        if formula.startswith(f"{tap.name}/") and not formula.endswith(f"@{version}"):
            raise ValueError(f"{formula} must be version-qualified as @${version}".replace("$", ""))
        if not formula.startswith(f"{tap.name}/"):
            raise ValueError(f"unsupported formula source: {formula}")
        packages.append(Package(
            name=raw["name"],
            formula=formula,
            version=version,
            binary=raw["binary"],
            version_args=tuple(raw["versionArgs"]),
            replaces=tuple(raw.get("replaces", [])),
            required=raw.get("required", True),
        ))
    if not packages:
        raise ValueError("host package manifest has no packages")
    return Manifest(tap=tap, packages=tuple(packages), notifier=notifier)


class Brew:
    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["brew", *args]
        if check:
            return subprocess.run(command, text=True, check=True)
        return subprocess.run(command, capture_output=True, text=True, check=False)


def ensure_tap(tap: Tap, brew: Brew) -> None:
    result = brew.run("tap")
    if result.returncode != 0:
        raise ReconcileError("cannot list Homebrew taps")
    if tap.name not in set(result.stdout.split()):
        brew.run("tap", tap.name, tap.url, check=True)


def sync_tap(
    tap: Tap,
    brew: Brew,
    repo_root: Path,
    *,
    run_git: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = lambda command: subprocess.run(
        command, text=True, check=False,
    ),
) -> None:
    tap_repo = brew.run("--repo", tap.name)
    if tap_repo.returncode != 0 or not tap_repo.stdout.strip():
        raise ReconcileError(f"cannot resolve repository for tap {tap.name}")
    commands = (
        ["git", "-C", tap_repo.stdout.strip(), "fetch", "--force", str(repo_root), "HEAD"],
        ["git", "-C", tap_repo.stdout.strip(), "checkout", "--detach", "--force", "FETCH_HEAD"],
    )
    for command in commands:
        result = run_git(command)
        if result.returncode != 0:
            raise ReconcileError(f"failed to synchronize tap {tap.name} to this checkout")


def _is_within(path: str, prefix: str) -> bool:
    try:
        return os.path.commonpath([path, prefix]) == prefix
    except ValueError:
        return False


def package_status(
    package: Package,
    brew: Brew,
    *,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
    resolve: Callable[[str], str] = lambda value: str(Path(value).resolve()),
    probe: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = lambda command: subprocess.run(
        command, capture_output=True, text=True, check=False,
    ),
) -> PackageStatus:
    prefix_result = brew.run("--prefix")
    if prefix_result.returncode != 0 or not prefix_result.stdout.strip():
        return PackageStatus(package, False, None, "cannot resolve Homebrew prefix")
    formula_opt = str(Path(prefix_result.stdout.strip()) / "opt" / package.short_formula)
    if not exists(formula_opt):
        return PackageStatus(package, False, None, "formula is not installed")
    prefix = resolve(formula_opt)

    binary = which(package.binary)
    if binary is None:
        return PackageStatus(package, False, None, "binary is not on PATH")
    resolved_binary = resolve(binary)
    if not _is_within(resolved_binary, prefix):
        return PackageStatus(package, False, None, f"binary is not owned by {package.short_formula}")

    result = probe([resolved_binary, *package.version_args])
    output = f"{result.stdout}\n{result.stderr}"
    observed_versions = re.findall(r"(?<![0-9.])v?(\d+\.\d+(?:\.\d+){0,2})(?![0-9.])", output)
    if result.returncode != 0 or not observed_versions:
        return PackageStatus(package, False, None, "version probe failed or returned an unrecognized version")
    if package.version in observed_versions:
        observed = package.version
    else:
        observed = observed_versions[0]
        return PackageStatus(package, False, observed, f"expected {package.version}, found {observed}")

    pinned = brew.run("list", "--pinned")
    pinned_names = set(pinned.stdout.split()) if pinned.returncode == 0 else set()
    if package.short_formula not in pinned_names:
        return PackageStatus(package, False, observed, "formula is not pinned")

    installed_replacements = _installed_replacements(package, brew)
    if installed_replacements:
        return PackageStatus(
            package,
            False,
            observed,
            "replacement is still installed: " + ", ".join(installed_replacements),
        )

    return PackageStatus(package, True, observed, "exact version installed, linked, and pinned")


class ReconcileError(RuntimeError):
    pass


class NotificationAuthorizationError(ReconcileError):
    pass


def _installed_formula_name(formula: str, brew: Brew) -> str | None:
    result = brew.run("list", "--versions", formula)
    if result.returncode == 0 and result.stdout.strip():
        return formula

    short_formula = formula.rsplit("/", 1)[-1]
    if short_formula == formula:
        return None
    result = brew.run("list", "--versions", short_formula)
    if result.returncode == 0 and result.stdout.strip():
        return short_formula
    return None


def _installed_replacements(package: Package, brew: Brew) -> list[str]:
    """Return installed replacement formulae using a resolvable Brew name.

    A removed tap formula can remain installed after its formula file disappears.
    Homebrew then rejects the original fully qualified name even though the keg is
    still addressable by its short name.
    """
    return [
        installed
        for formula in package.replaces
        if (installed := _installed_formula_name(formula, brew)) is not None
    ]


class MigrationError(ReconcileError):
    pass


def verify_app_bundle_signature(
    bundle: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    try:
        result = run(
            ["codesign", "--verify", "--deep", "--strict", bundle],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def sign_app_bundle(
    bundle: str,
    *,
    bundle_identifier: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    command = ["codesign", "--force", "--deep", "--sign", "-"]
    command.extend([
        "-r",
        f'=designated => identifier "{bundle_identifier}"',
    ])
    command.append(bundle)
    try:
        run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ReconcileError("codesign is required to sign managed application bundles") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ReconcileError(f"failed to sign application bundle {bundle}{suffix}") from exc


def verify_app_bundle_requirement(
    bundle: str,
    bundle_identifier: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    try:
        result = run(
            ["codesign", "--display", "-r-", bundle],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    expected = f'designated => identifier "{bundle_identifier}"'
    return result.returncode == 0 and expected in output


def notifier_source_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path in NOTIFIER_SOURCE_FILES:
        digest.update(relative_path.as_posix().encode())
        digest.update(b"\0")
        digest.update((repo_root / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def notifier_paths(install_root: Path = DEFAULT_NOTIFIER_INSTALL_ROOT) -> dict[str, Path]:
    bundle = install_root / NOTIFIER_BUNDLE_NAME
    return {
        "bundle": bundle,
        "binary": bundle / "Contents" / "MacOS" / NOTIFIER_BINARY_NAME,
        "digest": bundle / "Contents" / "Resources" / "source.sha256",
    }


def notifier_status(
    notifier: Notifier,
    repo_root: Path,
    *,
    install_root: Path = DEFAULT_NOTIFIER_INSTALL_ROOT,
    verify_signature: Callable[[str], bool] = verify_app_bundle_signature,
    verify_requirement: Callable[[str, str], bool] = verify_app_bundle_requirement,
    probe: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = lambda command: subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=5,
    ),
) -> NotifierStatus:
    paths = notifier_paths(install_root)
    if not paths["binary"].is_file() or not paths["digest"].is_file():
        return NotifierStatus(False, "application bundle is missing")
    try:
        installed_digest = paths["digest"].read_text().strip()
        expected_digest = notifier_source_digest(repo_root)
    except OSError as exc:
        return NotifierStatus(False, f"cannot read application bundle: {exc}")
    if installed_digest != expected_digest:
        return NotifierStatus(False, "installed application bundle is outdated")
    if not verify_signature(str(paths["bundle"])):
        return NotifierStatus(False, "application bundle signature is invalid")
    if not verify_requirement(str(paths["bundle"]), notifier.bundle_identifier):
        return NotifierStatus(False, "application bundle identity is unstable")

    try:
        version = probe([str(paths["binary"]), "version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return NotifierStatus(False, f"version probe failed: {exc}")
    expected = f"vicegerent-notifier {notifier.version}"
    if version.returncode != 0 or expected not in version.stdout:
        return NotifierStatus(False, "version probe failed")

    try:
        authorization = probe([str(paths["binary"]), "status"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return NotifierStatus(False, f"notification status probe failed: {exc}")
    if authorization.returncode != 0:
        detail = (authorization.stderr or authorization.stdout).strip()
        return NotifierStatus(False, detail or "notification authorization is unavailable")
    return NotifierStatus(True, "signed, registered, and authorized")


def _run_checked(
    command: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
    failure: str,
    timeout: float | None = None,
    error_type: type[ReconcileError] = ReconcileError,
) -> subprocess.CompletedProcess[str]:
    try:
        return run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise error_type(f"{failure}: command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise error_type(f"{failure}{suffix}") from exc
    except subprocess.TimeoutExpired as exc:
        raise error_type(f"{failure}: timed out waiting for macOS") from exc


def reconcile_notifier(
    notifier: Notifier,
    repo_root: Path,
    *,
    install_root: Path = DEFAULT_NOTIFIER_INSTALL_ROOT,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    verify_signature: Callable[[str], bool] = verify_app_bundle_signature,
    verify_requirement: Callable[[str, str], bool] = verify_app_bundle_requirement,
) -> None:
    install_root.parent.mkdir(parents=True, exist_ok=True)
    source = repo_root / "host" / "notifier" / "main.m"
    info_plist = repo_root / "host" / "notifier" / "Info.plist"
    icon = repo_root / "host" / "notifier" / "Vicegerent.icns"
    source_digest = notifier_source_digest(repo_root)

    with tempfile.TemporaryDirectory(
        prefix="vicegerent-notifier-",
        dir=install_root.parent,
    ) as temporary_directory:
        staging_root = Path(temporary_directory)
        bundle = staging_root / NOTIFIER_BUNDLE_NAME
        macos_dir = bundle / "Contents" / "MacOS"
        resources_dir = bundle / "Contents" / "Resources"
        macos_dir.mkdir(parents=True)
        resources_dir.mkdir(parents=True)
        shutil.copy2(info_plist, bundle / "Contents" / "Info.plist")
        shutil.copy2(icon, resources_dir / icon.name)
        (resources_dir / "source.sha256").write_text(source_digest + "\n")
        binary = macos_dir / NOTIFIER_BINARY_NAME
        module_cache = staging_root / "module-cache"

        _run_checked(
            [
                "xcrun", "clang", "-fobjc-arc", "-fmodules",
                f"-fmodules-cache-path={module_cache}",
                "-mmacosx-version-min=13.0", str(source), "-o", str(binary),
                "-framework", "Cocoa", "-framework", "UserNotifications",
            ],
            run=run,
            failure="failed to compile vicegerent-notifier",
        )
        sign_app_bundle(
            str(bundle),
            bundle_identifier=notifier.bundle_identifier,
            run=run,
        )
        if not verify_signature(str(bundle)):
            raise ReconcileError("vicegerent-notifier signature is invalid after signing")
        if not verify_requirement(str(bundle), notifier.bundle_identifier):
            raise ReconcileError("vicegerent-notifier identity is unstable after signing")

        version = _run_checked(
            [str(binary), "version"],
            run=run,
            failure="vicegerent-notifier version probe failed",
        )
        if f"vicegerent-notifier {notifier.version}" not in version.stdout:
            raise ReconcileError(
                f"vicegerent-notifier did not report expected version {notifier.version}"
            )

        install_root.mkdir(parents=True, exist_ok=True)
        installed_bundle = notifier_paths(install_root)["bundle"]
        if installed_bundle.exists():
            shutil.rmtree(installed_bundle)
        shutil.move(str(bundle), str(installed_bundle))

    _run_checked(
        [str(LSREGISTER), "-f", str(notifier_paths(install_root)["bundle"])],
        run=run,
        failure="failed to register vicegerent-notifier with LaunchServices",
    )
    print("Waiting for macOS notification permission; choose Allow if prompted...")
    authorization = _run_checked(
        [str(notifier_paths(install_root)["binary"]), "authorize"],
        run=run,
        failure="failed to authorize Vicegerent notifications",
        timeout=120,
        error_type=NotificationAuthorizationError,
    )
    if "authorized" not in authorization.stdout:
        raise ReconcileError("vicegerent-notifier authorization was not granted")
    _run_checked(
        [str(notifier_paths(install_root)["binary"]), "remove-all"],
        run=run,
        failure="failed to clear stale vicegerent-notifier notifications",
    )


def _probe_expected_version(
    package: Package,
    binary: str,
    probe: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    result = probe([binary, *package.version_args])
    output = f"{result.stdout}\n{result.stderr}"
    observed_versions = re.findall(r"(?<![0-9.])v?(\d+\.\d+(?:\.\d+){0,2})(?![0-9.])", output)
    if result.returncode != 0 or package.version not in observed_versions:
        raise ReconcileError(
            f"{package.name} version probe did not report expected {package.version}"
        )


def reconcile_package(
    package: Package,
    brew: Brew,
    *,
    probe: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = lambda command: subprocess.run(
        command, capture_output=True, text=True, check=False,
    ),
) -> None:
    installed = brew.run("list", "--versions", package.formula)
    if installed.returncode != 0:
        install_args = []
        if package.replaces:
            # Homebrew's own post-install auto-link runs before reconcile's
            # explicit unlink/link-force below, and fails outright if a
            # replaced formula's keg still owns the target symlink.
            install_args.append("--overwrite")
        brew.run("install", *install_args, package.formula, check=True)

    prefix_result = brew.run("--prefix", package.formula)
    if prefix_result.returncode != 0 or not prefix_result.stdout.strip():
        raise ReconcileError(f"cannot resolve installed prefix for {package.formula}")
    prefix = prefix_result.stdout.strip()
    desired_binary = str(Path(prefix) / "bin" / package.binary)

    _probe_expected_version(package, desired_binary, probe)

    installed_replacements = _installed_replacements(package, brew)
    unlinked: list[str] = []
    try:
        for formula in installed_replacements:
            brew.run("unlink", formula, check=True)
            unlinked.append(formula)
        brew.run("link", "--force", package.formula, check=True)
        brew.run("pin", package.formula, check=True)
    except subprocess.CalledProcessError as exc:
        brew.run("unlink", package.formula)
        rollback_failed = False
        for formula in unlinked:
            restored = brew.run("link", "--force", formula)
            rollback_failed = rollback_failed or restored.returncode != 0
        detail = "rollback failed" if rollback_failed else "previous links restored"
        raise MigrationError(f"failed to activate {package.formula}; {detail}") from exc

    pinned_names: set[str] = set()
    if installed_replacements:
        pinned = brew.run("list", "--pinned")
        if pinned.returncode != 0:
            raise ReconcileError("cannot list pinned Homebrew formulae")
        pinned_names = set(pinned.stdout.split())
    for formula in installed_replacements:
        if formula.rsplit("/", 1)[-1] in pinned_names:
            brew.run("unpin", formula, check=True)
        brew.run("uninstall", "--ignore-dependencies", formula, check=True)


def check(
    manifest: Manifest,
    brew: Brew,
    repo_root: Path = Path(__file__).resolve().parents[2],
    *,
    check_notifier: Callable[[Notifier, Path], NotifierStatus] = notifier_status,
) -> int:
    statuses, native_status = inspect_host_status(
        manifest,
        brew,
        repo_root,
        check_notifier=check_notifier,
    )
    for status in statuses:
        marker = "OK" if status.ok else ("DRIFT" if status.package.required else "OPTIONAL")
        observed = status.observed_version or "missing"
        print(f"{marker:5} {status.package.name}: expected {status.package.version}, observed {observed} - {status.detail}")
    marker = "OK" if native_status.ok else "DRIFT"
    print(
        f"{marker:5} vicegerent-notifier: expected {manifest.notifier.version} - "
        f"{native_status.detail}"
    )
    packages_ok = all(status.ok or not status.package.required for status in statuses)
    return 0 if packages_ok and native_status.ok else 1


def inspect_host_status(
    manifest: Manifest,
    brew: Brew,
    repo_root: Path,
    *,
    check_notifier: Callable[[Notifier, Path], NotifierStatus] | None = None,
) -> tuple[list[PackageStatus], NotifierStatus]:
    """Probe independent package and notifier state concurrently.

    Every task is read-only. Reconciliation remains serialized in `apply` so
    Homebrew linking, pinning, and uninstall operations cannot race each other.
    Results retain manifest order for stable diagnostics.
    """
    notifier_probe = check_notifier or notifier_status
    with ThreadPoolExecutor(
        max_workers=min(len(manifest.packages) + 1, 8),
        thread_name_prefix="host-package-check",
    ) as executor:
        package_futures = [
            executor.submit(package_status, package, brew)
            for package in manifest.packages
        ]
        notifier_future = executor.submit(notifier_probe, manifest.notifier, repo_root)
        statuses = [future.result() for future in package_futures]
        native_status = notifier_future.result()
    return statuses, native_status


def apply(manifest: Manifest, brew: Brew, repo_root: Path) -> int:
    package_statuses, current_notifier = inspect_host_status(
        manifest,
        brew,
        repo_root,
    )
    if any(not status.ok for status in package_statuses):
        ensure_tap(manifest.tap, brew)
        sync_tap(manifest.tap, brew, repo_root)
    for status in package_statuses:
        package = status.package
        if status.ok:
            print(f"{package.name} {package.version} is already reconciled.")
            continue
        print(f"Reconciling {package.name} {package.version}...")
        try:
            reconcile_package(package, brew)
        except (ReconcileError, subprocess.CalledProcessError) as exc:
            if package.required or isinstance(exc, MigrationError):
                raise
            print(f"WARNING: optional package {package.name} was not reconciled: {exc}", file=sys.stderr)
    if current_notifier.ok:
        print(f"vicegerent-notifier {manifest.notifier.version} is already reconciled.")
    else:
        print(f"Reconciling vicegerent-notifier {manifest.notifier.version}...")
        reconcile_notifier(manifest.notifier, repo_root)
    return check(manifest, brew, repo_root)


def _formula_class_name(short_formula: str) -> str:
    name, version = short_formula.split("@", 1)
    base = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)
    return f"{base}AT{re.sub(r'[^A-Za-z0-9]', '', version)}"


def validate_formulae(manifest: Manifest, repo_root: Path) -> int:
    errors: list[str] = []
    validated = 0
    for package in manifest.packages:
        validated += 1
        formula_path = repo_root / "Formula" / f"{package.short_formula}.rb"
        if not formula_path.is_file():
            errors.append(f"missing {formula_path.relative_to(repo_root)}")
            continue
        content = formula_path.read_text()
        required = (
            f"class {_formula_class_name(package.short_formula)} < Formula",
            f'version "{package.version}"',
            "keg_only :versioned_formula",
            "sha256 ",
        )
        for marker in required:
            if marker not in content:
                errors.append(f"{formula_path.name}: missing {marker}")
        if "latest" in content.lower() or re.search(r'^\s*head\s', content, re.MULTILINE):
            errors.append(f"{formula_path.name}: formula must use immutable release URLs only")
        if re.search(r"^\s*ldflags\s*=.*-X[A-Za-z0-9]", content, re.MULTILINE):
            errors.append(f"{formula_path.name}: Go -X linker flag requires a separate value")

    notifier_plist_path = repo_root / "host" / "notifier" / "Info.plist"
    notifier_source_path = repo_root / "host" / "notifier" / "main.m"
    notifier_icon_path = repo_root / "host" / "notifier" / "Vicegerent.icns"
    if not notifier_plist_path.is_file():
        errors.append("missing host/notifier/Info.plist")
    if not notifier_source_path.is_file():
        errors.append("missing host/notifier/main.m")
    if not notifier_icon_path.is_file():
        errors.append("missing host/notifier/Vicegerent.icns")
    if notifier_plist_path.is_file():
        with notifier_plist_path.open("rb") as plist_file:
            notifier_plist = plistlib.load(plist_file)
        if notifier_plist.get("CFBundleIdentifier") != manifest.notifier.bundle_identifier:
            errors.append("host/notifier/Info.plist bundle identifier does not match packages.json")
        if notifier_plist.get("CFBundleShortVersionString") != manifest.notifier.version:
            errors.append("host/notifier/Info.plist version does not match packages.json")
        if notifier_plist.get("CFBundleIconFile") != notifier_icon_path.name:
            errors.append("host/notifier/Info.plist icon does not match the managed icon")
    if notifier_source_path.is_file():
        source = notifier_source_path.read_text()
        if f'@"{manifest.notifier.version}"' not in source:
            errors.append("host/notifier/main.m version does not match packages.json")
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(
        f"OK - {validated} host package formulae and the native notifier "
        "match the exact-version manifest"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply", "validate"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--yes", action="store_true", help="apply without an interactive confirmation")
    return parser


def _print_diagnostic(label: str, message: object, color: str) -> None:
    if sys.stderr.isatty() and "NO_COLOR" not in os.environ:
        print(f"\033[{color}m{label}\033[0m {message}", file=sys.stderr)
    else:
        print(f"{label} {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.command == "validate":
        return validate_formulae(manifest, Path(__file__).resolve().parents[2])
    if shutil.which("brew") is None:
        print("Homebrew is required but brew is not on PATH", file=sys.stderr)
        return 1
    if args.command == "apply":
        if not args.yes:
            versions = ", ".join([
                *(f"{package.name}={package.version}" for package in manifest.packages),
                f"vicegerent-notifier={manifest.notifier.version}",
            ])
            replacements = sorted({
                formula
                for package in manifest.packages
                for formula in package.replaces
            })
            print(f"Will install and link managed Homebrew packages: {versions}")
            print(
                "Installed floating replacements will be unlinked and uninstalled: "
                + ", ".join(replacements)
            )
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                print(
                    "No interactive input available; re-run with --yes to accept the declared replacements.",
                    file=sys.stderr,
                )
                return 1
            if answer not in {"y", "yes"}:
                print("Aborted.", file=sys.stderr)
                return 1
        try:
            return apply(manifest, Brew(), Path(__file__).resolve().parents[2])
        except NotificationAuthorizationError as exc:
            _print_diagnostic("WARNING:", exc, "1;33")
            return 1
        except (ReconcileError, subprocess.CalledProcessError) as exc:
            _print_diagnostic("ERROR:", exc, "1;31")
            return 1
    return check(manifest, Brew())


if __name__ == "__main__":
    raise SystemExit(main())
