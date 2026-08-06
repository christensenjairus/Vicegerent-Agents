#!/usr/bin/env python3
"""Reconcile the exact Homebrew formula versions required by the host stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).with_name("packages.json")


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
    force_bottle: bool = False

    @property
    def short_formula(self) -> str:
        return self.formula.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Manifest:
    tap: Tap
    packages: tuple[Package, ...]


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
    packages: list[Package] = []
    for raw in data.get("packages", []):
        formula = raw["formula"]
        version = raw["version"]
        if formula.startswith(f"{tap.name}/") and not formula.endswith(f"@{version}"):
            raise ValueError(f"{formula} must be version-qualified as @${version}".replace("$", ""))
        if not (
            formula.startswith(f"{tap.name}/")
            or re.fullmatch(r"homebrew/core/[a-z0-9][a-z0-9-]*", formula)
        ):
            raise ValueError(f"unsupported formula source: {formula}")
        packages.append(Package(
            name=raw["name"],
            formula=formula,
            version=version,
            binary=raw["binary"],
            version_args=tuple(raw["versionArgs"]),
            replaces=tuple(raw.get("replaces", [])),
            required=raw.get("required", True),
            force_bottle=raw.get("forceBottle", False),
        ))
    if not packages:
        raise ValueError("host package manifest has no packages")
    return Manifest(tap=tap, packages=tuple(packages))


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

    installed_replacements = [
        formula for formula in package.replaces
        if (result := brew.run("list", "--versions", formula)).returncode == 0
        and result.stdout.strip()
    ]
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


class MigrationError(ReconcileError):
    pass


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
        install_args = ("--force-bottle",) if package.force_bottle else ()
        brew.run("install", *install_args, package.formula, check=True)

    prefix_result = brew.run("--prefix", package.formula)
    if prefix_result.returncode != 0 or not prefix_result.stdout.strip():
        raise ReconcileError(f"cannot resolve installed prefix for {package.formula}")
    desired_binary = str(Path(prefix_result.stdout.strip()) / "bin" / package.binary)
    _probe_expected_version(package, desired_binary, probe)

    installed_replacements = [
        formula for formula in package.replaces
        if brew.run("list", "--versions", formula).returncode == 0
    ]
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


def check(manifest: Manifest, brew: Brew) -> int:
    statuses = [package_status(package, brew) for package in manifest.packages]
    for status in statuses:
        marker = "OK" if status.ok else ("DRIFT" if status.package.required else "OPTIONAL")
        observed = status.observed_version or "missing"
        print(f"{marker:5} {status.package.name}: expected {status.package.version}, observed {observed} — {status.detail}")
    return 0 if all(status.ok or not status.package.required for status in statuses) else 1


def apply(manifest: Manifest, brew: Brew, repo_root: Path) -> int:
    ensure_tap(manifest.tap, brew)
    sync_tap(manifest.tap, brew, repo_root)
    for package in manifest.packages:
        print(f"Reconciling {package.name} {package.version}...")
        try:
            reconcile_package(package, brew)
        except (ReconcileError, subprocess.CalledProcessError) as exc:
            if package.required or isinstance(exc, MigrationError):
                raise
            print(f"WARNING: optional package {package.name} was not reconciled: {exc}", file=sys.stderr)
    return check(manifest, brew)


def _formula_class_name(short_formula: str) -> str:
    name, version = short_formula.split("@", 1)
    base = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)
    return f"{base}AT{re.sub(r'[^A-Za-z0-9]', '', version)}"


def validate_formulae(manifest: Manifest, repo_root: Path) -> int:
    errors: list[str] = []
    validated = 0
    for package in manifest.packages:
        if package.formula.startswith("homebrew/core/"):
            continue
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
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(f"OK - {validated} host package formulae match the exact-version manifest")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply", "validate"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--yes", action="store_true", help="apply without an interactive confirmation")
    return parser


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
            versions = ", ".join(f"{p.name}={p.version}" for p in manifest.packages)
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
        except (ReconcileError, subprocess.CalledProcessError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    return check(manifest, Brew())


if __name__ == "__main__":
    raise SystemExit(main())
