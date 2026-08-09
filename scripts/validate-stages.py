#!/usr/bin/env python3
"""Validate the staged installer manifest before an installation mutates a cluster.

The default mode is intentionally static and side-effect-free: it validates the
manifest schema, immutable references, and repository-local paths without
fetching charts, cloning repositories, or contacting a cluster.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import cast

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]
ACTION_FIELDS = {
    "helm": {"name", "type", "repo", "chart", "version", "namespace", "values", "crds", "contextValue"},
    "helm-oci": {
        "name",
        "type",
        "chart",
        "version",
        "namespace",
        "values",
        "crds",
        "replicasValuePath",
        "replicasSetKey",
    },
    "helm-git": {"name", "type", "gitRepo", "ref", "chartPath", "namespace", "values", "crds"},
    "local": {"name", "type", "namespace", "machineValues", "forEach"},
    "kubectl-k": {"name", "type", "path", "gate", "waitResource", "waitNamespace"},
}
REQUIRED_FIELDS = {
    "helm": {"name", "type", "repo", "chart", "version", "namespace"},
    "helm-oci": {"name", "type", "chart", "version", "namespace"},
    "helm-git": {"name", "type", "gitRepo", "ref", "chartPath", "namespace"},
    "local": {"name", "type", "namespace"},
    "kubectl-k": {"name", "type", "path"},
}
IMMUTABLE_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
IMMUTABLE_GIT_REF = re.compile(r"^(?:v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?|[0-9a-f]{40})$")


def fail(message: str) -> None:
    print(f"ERROR - {message}", file=sys.stderr)
    raise SystemExit(1)


def require_string(action: Mapping[str, object], field: str, location: str) -> str:
    value = action.get(field)
    if not isinstance(value, str) or not value:
        fail(f"{location}: {field} must be a non-empty string")
    return value


def validate_action(action: object, location: str, repo_root: pathlib.Path) -> None:
    if not isinstance(action, Mapping):
        fail(f"{location}: action must be a mapping")
    action = cast(Mapping[str, object], action)
    kind = action.get("type")
    if not isinstance(kind, str) or kind not in ACTION_FIELDS:
        fail(f"{location}: unsupported action type {kind!r}")
        return
    unknown = set(action) - ACTION_FIELDS[kind]
    if unknown:
        fail(f"{location}: unknown fields for {kind}: {', '.join(sorted(unknown))}")
    missing = REQUIRED_FIELDS[kind] - set(action)
    if missing:
        fail(f"{location}: missing required fields for {kind}: {', '.join(sorted(missing))}")
    for field in REQUIRED_FIELDS[kind]:
        require_string(action, field, location)

    if kind in {"helm", "helm-oci"}:
        version = require_string(action, "version", location)
        if not IMMUTABLE_VERSION.fullmatch(version):
            fail(f"{location}: version must be an immutable semantic release, not {version!r}")
    if kind == "helm":
        repo = require_string(action, "repo", location)
        if not repo.startswith("https://"):
            fail(f"{location}: helm repo must use https")
        if "contextValue" in action:
            require_string(action, "contextValue", location)
    if kind == "helm-oci" and not require_string(action, "chart", location).startswith("oci://"):
        fail(f"{location}: helm-oci chart must start with oci://")
    if kind == "helm-oci":
        replica_fields = {field for field in ("replicasValuePath", "replicasSetKey") if field in action}
        if replica_fields and len(replica_fields) != 2:
            fail(f"{location}: replicasValuePath and replicasSetKey must be set together")
        for field in replica_fields:
            require_string(action, field, location)
    if kind == "helm-git":
        ref = require_string(action, "ref", location)
        if not IMMUTABLE_GIT_REF.fullmatch(ref):
            fail(f"{location}: git ref must be an immutable tag or 40-character commit, not {ref!r}")
        if not require_string(action, "gitRepo", location).startswith("https://"):
            fail(f"{location}: gitRepo must use https")
    if kind == "kubectl-k":
        relative_path = pathlib.PurePosixPath(require_string(action, "path", location))
        if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.parts[:2] != ("stages", "kustomize"):
            fail(f"{location}: kustomize path must stay under stages/kustomize")
        path = repo_root.joinpath(*relative_path.parts)
        kustomize_root = (repo_root / "stages" / "kustomize").resolve()
        try:
            path.resolve().relative_to(kustomize_root)
        except ValueError:
            fail(f"{location}: kustomize path must stay under stages/kustomize")
        if not path.is_dir():
            fail(f"{location}: kustomize path does not exist: {path.relative_to(repo_root)}")
        gate = action.get("gate")
        if gate not in {None, "established", "rollout"}:
            fail(f"{location}: unsupported readiness gate {gate!r}")
        if gate == "rollout":
            require_string(action, "waitResource", location)
            require_string(action, "waitNamespace", location)
    if kind == "local":
        name = require_string(action, "name", location)
        path = repo_root / "charts" / name
        if repo_root == REPO and not path.is_dir():
            fail(f"{location}: local chart does not exist: charts/{name}")
        machine_values = action.get("machineValues")
        for_each = action.get("forEach")
        valid_mode = (machine_values == "full" and for_each is None) or (
            machine_values is None and for_each == "agents"
        )
        if not valid_mode:
            fail(f"{location}: local action needs exactly machineValues: full or forEach: agents")
    if "crds" in action and not isinstance(action["crds"], bool):
        fail(f"{location}: crds must be a boolean")
    values = action.get("values")
    if values is not None:
        values_name = require_string(action, "values", location)
        if pathlib.PurePath(values_name).name != values_name:
            fail(f"{location}: values must name a file under stages/values")
        path = repo_root / "stages" / "values" / values_name
        if repo_root == REPO and not path.is_file():
            fail(f"{location}: values file does not exist: stages/values/{values_name}")


def validate(stages_path: pathlib.Path) -> None:
    try:
        document = yaml.safe_load(stages_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        fail(f"cannot read {stages_path}: {exc}")
    if not isinstance(document, Mapping) or set(document) != {"stages"}:
        fail("stage manifest must contain only a top-level stages list")
    stages = document["stages"]
    if not isinstance(stages, list) or not stages:
        fail("stages must be a non-empty list")
    names: set[str] = set()
    repo_root = REPO if stages_path.resolve() == (REPO / "stages" / "stages.yaml").resolve() else stages_path.parent.parent
    for stage_index, stage in enumerate(stages):
        location = f"stages[{stage_index}]"
        if not isinstance(stage, Mapping) or set(stage) != {"name", "actions"}:
            fail(f"{location}: stage must contain only name and actions")
        name = require_string(stage, "name", location)
        if name in names:
            fail(f"{location}: duplicate stage name {name!r}")
        names.add(name)
        actions = stage["actions"]
        if not isinstance(actions, list) or not actions:
            fail(f"{location}: actions must be a non-empty list")
        action_names: set[str] = set()
        for action_index, action in enumerate(actions):
            action_location = f"{location}.actions[{action_index}]"
            validate_action(action, action_location, repo_root)
            action_name = action["name"]
            if action_name in action_names:
                fail(f"{action_location}: duplicate action name {action_name!r} in stage {name!r}")
            action_names.add(action_name)
    print(f"OK - validated {len(stages)} stages in {stages_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stages", type=pathlib.Path, default=REPO / "stages" / "stages.yaml")
    parser.add_argument("--static-only", action="store_true", help="accepted for explicit CI/documentation clarity")
    args = parser.parse_args()
    validate(args.stages)


if __name__ == "__main__":
    main()
