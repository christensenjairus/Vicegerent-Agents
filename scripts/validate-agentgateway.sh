#!/usr/bin/env bash
# Validate rendered agentgateway custom resources against the CRD schema pinned
# in stages/stages.yaml, so the schema can never drift from the version the
# installer actually deploys. See scripts/validate-agentgateway-crds.py for why
# this gate exists (kubeconform runs -ignore-missing-schemas, so a malformed
# guardrail block would otherwise load silently and leave secret reads allowed).
set -o errexit
set -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STAGES="stages/stages.yaml"
DEFAULTS_VALUES="values.defaults.yaml"
EXAMPLE_VALUES="values.example.yaml"
SECRET_PATTERNS_FILE="images/mcp-cerbos-shim/internal/server/secret-patterns.json"  # pragma: allowlist secret (a file path, not a secret)
CRD_VERSION="$(yq '.stages[].actions[] | select(.name == "agentgateway-crds") | .version' "$STAGES")"
[[ -n "$CRD_VERSION" && "$CRD_VERSION" != "null" ]] \
  || { echo "ERROR - agentgateway-crds version not found in ${STAGES}" >&2; exit 1; }
echo "INFO - agentgateway-crds version pinned to ${CRD_VERSION} (from ${STAGES})"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Pull + unpack the same CRD chart the installer deploys.
helm pull "oci://cr.agentgateway.dev/charts/agentgateway-crds" \
  --version "${CRD_VERSION}" -d "$WORK" >/dev/null
tar xzf "$WORK"/agentgateway-crds-*.tgz -C "$WORK"
CRD_GLOB="$WORK/agentgateway-crds/templates/*.yaml"

# charts/platform carries every agentgateway custom resource: the Gateway's
# AgentgatewayParameters, the model backends/routes, and the vMCP backend/route/
# policy. Render it and hand the whole stream to the validator, which validates
# only the agentgateway.dev/* resources and ignores the rest.
RENDERED="$WORK/rendered.yaml"
helm template platform charts/platform -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" \
  --set-file "secretPatterns=$SECRET_PATTERNS_FILE" > "$RENDERED"

python3 scripts/validate-agentgateway-crds.py "$CRD_GLOB" "$RENDERED"
