#!/usr/bin/env python3
"""Validate rendered agentgateway custom resources against the pinned
agentgateway CRD openAPIV3Schema.

Why this exists: kubeconform runs with -ignore-missing-schemas, so
AgentgatewayPolicy/AgentgatewayBackend resources are otherwise NOT validated.
A malformed `backend.mcp.guardrails` block silently fails to load, leaving only
the tool-name allowlist active and secret reads ALLOWED. This gate fails closed
on the resources it owns: a rendered resource in one of the CRD chart's own API
groups whose (group, version, kind) has no matching schema is an ERROR, not a
skip, so a renamed kind or a bumped apiVersion can't slip through unvalidated.
Resources in every other group are skipped -- kubeconform validates those.

Limits (documented, not silent): JSON-schema cannot evaluate the CRD's
x-kubernetes-validations CEL rules. Kubernetes structural schemas nevertheless
reject undeclared fields during typed patch construction even when generated
CRDs omit explicit additionalProperties:false. This validator mirrors that
behavior for objects with declared properties, while preserving explicit maps
and x-kubernetes-preserve-unknown-fields subtrees.

Usage: validate-agentgateway-crds.py <crd-glob> <rendered.yaml> [<rendered.yaml> ...]
"""
import sys
import glob
import yaml

try:
    import jsonschema
except ImportError:
    print("ERROR - python 'jsonschema' not installed", file=sys.stderr)
    sys.exit(2)


def close_structural_objects(node):
    """Mirror Kubernetes typed-object handling for generated CRD schemas.

    jsonschema permits undeclared object keys unless additionalProperties is
    explicitly false. Kubernetes' structured-merge-diff type converter does
    not: a typed Helm patch fails when a manifest contains a field absent from
    an object's declared properties. Generated CRDs commonly omit the JSON
    Schema keyword, so add it in-memory for declared structural objects only.
    """
    if isinstance(node, list):
        for item in node:
            close_structural_objects(item)
        return
    if not isinstance(node, dict):
        return

    properties = node.get("properties")
    if (
        node.get("type") == "object"
        and isinstance(properties, dict)
        and properties
        and node.get("x-kubernetes-preserve-unknown-fields") is not True
        and "additionalProperties" not in node
    ):
        node["additionalProperties"] = False

    for value in node.values():
        close_structural_objects(value)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    crd_glob = sys.argv[1]
    targets = sys.argv[2:]

    schemas = {}
    for f in glob.glob(crd_glob):
        with open(f) as fh:
            for doc in yaml.safe_load_all(fh):
                if not doc or doc.get("kind") != "CustomResourceDefinition":
                    continue
                group = doc["spec"]["group"]
                kind = doc["spec"]["names"]["kind"]
                for v in doc["spec"]["versions"]:
                    schema = v["schema"]["openAPIV3Schema"]
                    close_structural_objects(schema)
                    schemas[(group, v["name"], kind)] = schema

    if not schemas:
        print(f"ERROR - no CRD schemas loaded from {crd_glob}", file=sys.stderr)
        return 3
    print(f"INFO - loaded {len(schemas)} CRD version schema(s)")
    owned_groups = {group for group, _, _ in schemas}

    errors = 0
    checked = 0
    for tf in targets:
        with open(tf) as fh:
            for doc in yaml.safe_load_all(fh):
                if not doc:
                    continue
                av = doc.get("apiVersion", "")
                kind = doc.get("kind", "")
                if "/" not in av:
                    continue
                group, version = av.split("/", 1)
                key = (group, version, kind)
                if key not in schemas:
                    if group in owned_groups:
                        errors += 1
                        print(
                            f"FAIL - {tf}: {kind} {av} is in the agentgateway-managed "
                            f"group '{group}' but the pinned CRD chart ships no schema "
                            "for that version/kind; the resource would deploy "
                            "unvalidated",
                            file=sys.stderr,
                        )
                    continue  # other groups: kubeconform validates those
                checked += 1
                try:
                    jsonschema.validate(doc, schemas[key])
                    print(f"PASS - {tf}: {kind} {av}")
                except jsonschema.ValidationError as e:
                    errors += 1
                    loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
                    print(f"FAIL - {tf}: {kind} {av} at {loc}: {e.message}", file=sys.stderr)

    if checked == 0:
        print("ERROR - no agentgateway CRD resources matched; schema wiring broken", file=sys.stderr)
        return 3
    print(f"INFO - validated {checked} agentgateway CRD resource(s), {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
