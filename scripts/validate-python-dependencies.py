#!/usr/bin/env python3
"""Validate the repository's single locked Python tool environment."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (ROOT / "scripts", ROOT / "host" / "brew", ROOT / "host" / "mcp")
SCAN_FILES = (
    ROOT / "images" / "agent" / "patches" / "tests" / "test_hermes_home_migration.py",
    ROOT / "images" / "agent" / "patches" / "tests" / "test_provider_reasoning_overrides.py",
)
IMPORT_DISTRIBUTIONS = {
    "jsonschema": "jsonschema",
    "rich": "rich",
    "textual": "textual",
    "yaml": "pyyaml",
}
RUNTIME_ONLY_IMPORTS = {"agent", "agentburn", "hermes_cli"}
REQUIRED_TOOLS = {"detect-secrets", "pre-commit", "uv"}


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    result: dict[str, str] = {}
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", requirement)
        if not match:
            raise SystemExit(f"FAIL - dependency is not exactly pinned: {requirement}")
        result[normalized(match.group(1))] = match.group(2)
    return result


def external_imports() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    paths = [
        path
        for root in SCAN_ROOTS
        for path in root.rglob("*.py")
        if not {".venv", "venv", "site-packages"}.intersection(path.relative_to(root).parts)
    ] + list(SCAN_FILES)
    local_modules = {path.stem.replace("-", "_") for path in paths}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
        unknown = sorted(
            name
            for name in imports
            if name not in sys.stdlib_module_names
            and name not in local_modules
            and name not in RUNTIME_ONLY_IMPORTS
        )
        if unknown:
            result[str(path.relative_to(ROOT))] = unknown
    return result


def main() -> int:
    declared = declared_dependencies()
    imports = external_imports()
    unknown = {
        path: names
        for path, names in imports.items()
        if any(name not in IMPORT_DISTRIBUTIONS for name in names)
    }
    if unknown:
        details = "\n".join(f"{path}: {', '.join(names)}" for path, names in unknown.items())
        raise SystemExit(f"FAIL - unclassified third-party Python imports:\n{details}")

    required = REQUIRED_TOOLS | {
        IMPORT_DISTRIBUTIONS[name]
        for names in imports.values()
        for name in names
    }
    missing = sorted(required - declared.keys())
    if missing:
        raise SystemExit("FAIL - pyproject.toml is missing: " + ", ".join(missing))

    print(f"PASS - one locked Python environment covers {len(required)} direct dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
