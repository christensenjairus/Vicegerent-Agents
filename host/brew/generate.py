#!/usr/bin/env python3
"""Generate immutable Homebrew formulae after Renovate updates desired versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MANIFEST = Path(__file__).with_name("packages.json")
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "vicegerent-host-brew-generator/1"


class Fetcher:
    def bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()

    def json(self, url: str) -> dict[str, Any]:
        return json.loads(self.bytes(url))


def _formula_class_name(short_formula: str) -> str:
    name, version = short_formula.split("@", 1)
    base = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part)
    return f"{base}AT{re.sub(r'[^A-Za-z0-9]', '', version)}"


def _reject_formula_class_collision(repo_root: Path, short_formula: str) -> None:
    expected_path = repo_root / "Formula" / f"{short_formula}.rb"
    expected_class = _formula_class_name(short_formula)
    for formula_path in (repo_root / "Formula").glob("*.rb"):
        if formula_path == expected_path:
            continue
        match = re.search(r"^class\s+(\w+)\s+<\s+Formula$", formula_path.read_text(), re.MULTILINE)
        if match and match.group(1) == expected_class:
            raise ValueError(
                f"formula class {expected_class} for {short_formula} collides with "
                f"{formula_path.relative_to(repo_root)}"
            )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _github_source(package: dict[str, Any]) -> str:
    source = package.get("source", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source):
        raise ValueError(f"invalid GitHub source for {package.get('name')}: {source}")
    return source


def _validate_renovate_metadata(package: dict[str, Any], generator_type: str) -> None:
    datasource = package.get("renovateDatasource")
    dependency = package.get("renovateDependency")
    if datasource is None and dependency is None:
        return
    expected_datasource = "pypi" if generator_type == "pypi-sdist" else "github-releases"
    expected_dependency = (
        package.get("generator", {}).get("project")
        if generator_type == "pypi-sdist"
        else package.get("source")
    )
    if datasource != expected_datasource or dependency != expected_dependency:
        raise ValueError(
            f"Renovate metadata for {package['name']} must use "
            f"{expected_datasource}:{expected_dependency}"
        )


def _generator_tokens(package: dict[str, Any], fetcher: Fetcher) -> dict[str, str]:
    version = package["version"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version):
        raise ValueError(f"invalid release version for {package['name']}: {version}")
    generator = package.get("generator", {})
    kind = generator.get("type")
    _validate_renovate_metadata(package, kind)
    tag_prefix = generator.get("tagPrefix", "")
    if tag_prefix not in {"", "v"}:
        raise ValueError(f"invalid tag prefix for {package['name']}: {tag_prefix}")
    tokens = {"VERSION": version}

    if kind == "github-archive":
        source = _github_source(package)
        tag = f"{tag_prefix}{version}"
        url = f"https://github.com/{source}/archive/refs/tags/{tag}.tar.gz"
        tokens.update(URL=url, SHA256=_sha256(fetcher.bytes(url)))
    elif kind == "github-release-assets":
        source = _github_source(package)
        tag = f"{tag_prefix}{version}"
        assets = generator.get("assets", {})
        if not assets:
            raise ValueError(f"{package['name']} has no release assets")
        for key, pattern in assets.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError(f"invalid asset key for {package['name']}: {key}")
            asset = pattern.format(version=version)
            if not re.fullmatch(r"[A-Za-z0-9._-]+", asset):
                raise ValueError(f"invalid asset filename for {package['name']}: {asset}")
            url = f"https://github.com/{source}/releases/download/{tag}/{asset}"
            tokens[f"URL_{key}"] = url
            tokens[f"SHA_{key}"] = _sha256(fetcher.bytes(url))
    elif kind == "pypi-sdist":
        project = generator.get("project", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
            raise ValueError(f"invalid PyPI project for {package['name']}: {project}")
        api_url = f"https://pypi.org/pypi/{project}/{version}/json"
        releases = [item for item in fetcher.json(api_url).get("urls", []) if item.get("packagetype") == "sdist"]
        if len(releases) != 1:
            raise ValueError(f"expected one PyPI sdist for {project} {version}, found {len(releases)}")
        release = releases[0]
        sha256 = release.get("digests", {}).get("sha256")
        release_url = release.get("url", "")
        parsed_url = urlsplit(release_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "files.pythonhosted.org"
            or parsed_url.username is not None
            or parsed_url.password is not None
            or not parsed_url.path.startswith("/packages/")
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(f"invalid PyPI sdist URL for {project} {version}: {release_url}")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise ValueError(f"PyPI sdist metadata is incomplete for {project} {version}")
        downloaded_sha256 = _sha256(fetcher.bytes(release_url))
        if downloaded_sha256 != sha256:
            raise ValueError(f"PyPI sdist digest mismatch for {project} {version}")
        tokens.update(URL=release_url, SHA256=downloaded_sha256)
    else:
        raise ValueError(f"unsupported generator type for {package['name']}: {kind}")

    return tokens


def _render_formula(package: dict[str, Any], short_formula: str, repo_root: Path, fetcher: Fetcher) -> str:
    template_path = repo_root / "host" / "brew" / "templates" / f"{package['name']}.rb.in"
    if not template_path.is_file():
        raise ValueError(f"missing formula template {template_path.relative_to(repo_root)}")
    tokens = _generator_tokens(package, fetcher)
    tokens["CLASS_NAME"] = _formula_class_name(short_formula)
    content = template_path.read_text()
    for key, value in tokens.items():
        content = content.replace(f"@{key}@", value)
    unresolved = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]+@", content)))
    if unresolved:
        raise ValueError(f"unresolved tokens in {template_path.name}: {', '.join(unresolved)}")
    return content


def validate_formula_change_scope(manifest: dict[str, Any], changed_paths: list[str]) -> None:
    allowed = {
        f"Formula/{package['formula'].rsplit('/', 1)[1]}.rb"
        for package in manifest.get("packages", [])
    }
    unexpected = sorted(set(changed_paths) - allowed)
    if unexpected:
        raise ValueError(
            "formula changes must target current manifest formulae only: " + ", ".join(unexpected)
        )


def validate_formula_changes_since(manifest_path: Path, repo_root: Path, base_revision: str) -> None:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_revision}...HEAD", "--", "Formula"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    validate_formula_change_scope(json.loads(manifest_path.read_text()), result.stdout.splitlines())


def generate_updates(
    manifest_path: Path = DEFAULT_MANIFEST,
    repo_root: Path = DEFAULT_REPO_ROOT,
    *,
    fetcher: Fetcher | None = None,
    verify: bool = False,
) -> list[Path]:
    fetcher = fetcher or Fetcher()
    manifest_content = manifest_path.read_text()
    data = json.loads(manifest_content)
    changed: list[Path] = []
    formula_replacements: list[tuple[str, str]] = []

    for package in data.get("packages", []):
        formula = package["formula"]
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[a-z0-9][a-z0-9-]*@[0-9]+(?:\.[0-9]+){1,3}",
            formula,
        ):
            raise ValueError(f"invalid formula reference for {package.get('name')}: {formula}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", package.get("name", "")):
            raise ValueError(f"invalid package name: {package.get('name')}")
        formula_prefix, current_short = formula.rsplit("/", 1)
        base_name = current_short.split("@", 1)[0]
        expected_short = f"{base_name}@{package['version']}"
        expected_formula = f"{formula_prefix}/{expected_short}"
        formula_path = repo_root / "Formula" / f"{expected_short}.rb"
        _reject_formula_class_collision(repo_root, expected_short)
        if not verify and formula == expected_formula and formula_path.is_file():
            continue

        content = _render_formula(package, expected_short, repo_root, fetcher)
        if verify:
            if formula != expected_formula:
                raise ValueError(f"{package['name']} manifest formula does not match version {package['version']}")
            if not formula_path.is_file() or formula_path.read_text() != content:
                raise ValueError(f"{formula_path.relative_to(repo_root)} does not match regenerated content")
            continue

        if formula_path.exists():
            if formula_path.read_text() != content:
                raise ValueError(f"refusing to overwrite immutable formula {formula_path.relative_to(repo_root)}")
        else:
            formula_path.parent.mkdir(parents=True, exist_ok=True)
            formula_path.write_text(content)
            changed.append(formula_path)
        if formula != expected_formula:
            package["formula"] = expected_formula
            formula_replacements.append((formula, expected_formula))

    if formula_replacements:
        updated_manifest = manifest_content
        for old_formula, new_formula in formula_replacements:
            old = f'"formula": "{old_formula}"'
            new = f'"formula": "{new_formula}"'
            if updated_manifest.count(old) != 1:
                raise ValueError(f"cannot uniquely update manifest formula {old_formula}")
            updated_manifest = updated_manifest.replace(old, new, 1)
        manifest_path.write_text(updated_manifest)
        changed.append(manifest_path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--verify", action="store_true", help="redownload artifacts and compare generated formulae")
    parser.add_argument(
        "--changed-since",
        metavar="REVISION",
        help="reject Formula changes outside current manifest formulae",
    )
    args = parser.parse_args()
    if args.changed_since:
        validate_formula_changes_since(args.manifest, args.repo_root, args.changed_since)
    changed = generate_updates(args.manifest, args.repo_root, verify=args.verify)
    if args.verify:
        print("Managed Homebrew formulae match freshly downloaded artifacts")
        return 0
    if changed:
        print("Generated managed Homebrew updates:")
        for path in changed:
            print(f"  {path.relative_to(args.repo_root)}")
    else:
        print("Managed Homebrew formulae already match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
