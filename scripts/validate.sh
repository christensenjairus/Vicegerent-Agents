#!/usr/bin/env bash

# Static validation for the Helm-based install (no live cluster). Runs Helm
# lint/template/kubeconform and Cerbos compile checks across every chart, plus a
# growing set of repo-specific guards (image tags, model pricing/routing, MCP and
# LSP wiring, egress redaction, Homebrew reconciliation, and more). Each
# check announces itself with an "echo \"INFO - ...\"" line below -- read those
# for the current, authoritative list rather than relying on this header.

set -o errexit
set -o pipefail

RETRIES=5
WAIT=2
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=lib/python-env.sh
source "$REPO_ROOT/scripts/lib/python-env.sh"

EXAMPLE_VALUES="values.example.yaml"
DEFAULTS_VALUES="values.defaults.yaml"
# Canonical secret-pattern source injected into the egress-proxy scrubber via
# --set-file (the Go shim embeds the same file). Every egress-proxy render below
# must pass it or the chart's `required` guard fails by design.
SECRET_PATTERNS_FILE="$REPO_ROOT/images/mcp-cerbos-shim/internal/server/secret-patterns.json"
KUBECONFORM_CACHE="${KUBECONFORM_CACHE:-/tmp/.kubeconform-cache}"
kubeconform_flags=(-strict -ignore-missing-schemas -summary -cache "$KUBECONFORM_CACHE")

# One parent-scope scratch dir, cleaned as a whole: mktemp_* run inside
# subshells, so an array they populated there would never reach this scope.
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
mktemp_f() { mktemp "$WORKDIR/f.XXXXXX"; }
mktemp_d() { mktemp -d "$WORKDIR/d.XXXXXX"; }

retry_cmd() {
  local n=1
  until "$@"; do
    if (( n >= RETRIES )); then
      echo "ERROR - Command failed after $n attempts: $*"
      return 1
    fi
    echo "WARN - Attempt $n/$RETRIES failed. Retrying in ${WAIT}s..."
    sleep "$WAIT"
    n=$((n+1))
  done
}

require_tools() {
  local missing=0
  for cmd in helm yq kubeconform python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "ERROR - $cmd is not installed" >&2
      missing=1
    fi
  done
  (( missing == 0 )) || exit 1
  ensure_python_environment "$REPO_ROOT"
  PYTHON="$REPO_ROOT/.venv/bin/python"
  # yq below is invoked bare (no -r) in places, relying on mikefarah/yq v4's
  # default of printing plain scalars unquoted. A same-named PyPI package
  # (kislyuk/yq, a jq wrapper) JSON-quotes every string instead and silently
  # corrupts those checks.
  local yq_version_output
  yq_version_output="$(yq --version 2>&1)"
  if [[ "$yq_version_output" != *"mikefarah/yq"* ]]; then
    echo "ERROR - yq on PATH is not mikefarah/yq (got: '$yq_version_output'); a PATH entry ahead of it is shadowing the Go binary from https://github.com/mikefarah/yq — commonly a pip-installed 'yq' (kislyuk/yq) in a venv's bin dir" >&2
    exit 1
  fi
}

# Re-root agentDefaults and the first example agent the way the installer feeds the agent chart.
agent0_slice() { local f; f="$(mktemp_f)"; yq '.agents[0]' "$EXAMPLE_VALUES" > "$f"; printf '%s' "$f"; }
agent_defaults_slice() { local f; f="$(mktemp_f)"; yq '.agentDefaults' "$DEFAULTS_VALUES" > "$f"; printf '%s' "$f"; }

# Render the cerbos-policies ConfigMap and explode each data entry back into a
# standalone policy file, so `cerbos compile` sees the real substituted CEL (it has
# no concept of Helm templating and would choke on the raw chart sources).
render_cerbos_defs() {
  local values="$1" out="$2" cm k
  cm="$(helm template cerbos-policies charts/cerbos-policies -f "$DEFAULTS_VALUES" -f "$values")"
  while IFS= read -r k; do
    [[ -n "$k" ]] || continue
    k="$k" yq 'select(.kind=="ConfigMap") | .data[env(k)]' <<<"$cm" > "$out/$k"
  done < <(yq 'select(.kind=="ConfigMap") | .data | keys | .[]' <<<"$cm")
}

# Unmatched methods bypass processors, so pin every security-relevant phase.
# The internal route uses a request catchall. The three methods agentgateway
# v1.4.1 cannot process on the request phase run Response only and are denied by
# the shim before their bodies are inspected.
assert_guardrail_well_formed() {
  local rendered="$1" internal_target internal_route internal_policy_target
  assert_policy_guardrail "$rendered" vmcp-mcp-tools Full Response Response Off "prompts/get,resources/read,tasks/cancel,tasks/get,tasks/update,tools/call"
  assert_internal_policy_guardrail "$rendered"

  internal_target="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayBackend" and .metadata.name == "vmcp-internal")
    | select((.spec.mcp.targets | length) == 1)
    | .spec.mcp.targets[0].name' -)"
  internal_route="$(echo "$rendered" | yq ea '
    select(.kind == "HTTPRoute" and .metadata.name == "vmcp-internal")
    | select((.spec.parentRefs | length) == 1 and .spec.parentRefs[0].sectionName == "internal")
    | select((.spec.rules | length) == 1 and (.spec.rules[0].backendRefs | length) == 1)
    | .spec.rules[0].backendRefs[0].name' -)"
  internal_policy_target="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayPolicy" and .metadata.name == "vmcp-internal-mcp-tools")
    | select((.spec.targetRefs | length) == 1)
    | .spec.targetRefs[0].name' -)"
  if [[ "$internal_target" != "vmcp-internal" || "$internal_route" != "vmcp-internal" || "$internal_policy_target" != "vmcp-internal" ]]; then
    echo "ERROR - vmcp-internal must use the distinct MCP target name vmcp-internal and attach its route and policy only to the internal backend." >&2
    exit 1
  fi
}

assert_internal_policy_guardrail() {
  local rendered="$1" processors well_formed
  processors="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayPolicy" and .metadata.name == "vmcp-internal-mcp-tools")
    | .spec.backend.mcp.guardrails.processors // [] | length' -)"
  well_formed="$(echo "$rendered" | yq ea '
    select(.kind == "AgentgatewayPolicy" and .metadata.name == "vmcp-internal-mcp-tools")
    | [ .spec.backend.mcp.guardrails.processors[]
        | select(.methods["*"] == "Request"
            and .methods["resources/subscribe"] == "Response"
            and .methods["resources/unsubscribe"] == "Response"
            and .methods["completion/complete"] == "Response"
            and (.methods | keys | sort | join(",")) == "*,completion/complete,resources/subscribe,resources/unsubscribe"
            and .remote.backendRef.name == "mcp-cerbos-shim"
            and .remote.backendRef.namespace == "cerbos"
            and .remote.backendRef.port == 4445
            and .remote.failureMode == "FailClosed") ]
    | length' -)"
  if [[ "$processors" != "1" || "$well_formed" != "1" ]]; then
    echo "ERROR - vmcp-internal-mcp-tools must have one FailClosed shim processor with a Request catchall and Response-only denials for agentgateway v1.4.1's three request-phase-unsupported methods." >&2
    exit 1
  fi
}

assert_policy_guardrail() {
  local rendered="$1" name="$2" tools_phase="$3" task_get_phase="$4" task_update_phase="$5" task_cancel_phase="$6" expected_methods="$7" processors well_formed
  processors="$(echo "$rendered" | NAME="$name" yq ea '
    select(.kind == "AgentgatewayPolicy" and .metadata.name == strenv(NAME))
    | .spec.backend.mcp.guardrails.processors // [] | length' -)"
  well_formed="$(echo "$rendered" | NAME="$name" TOOLS_PHASE="$tools_phase" TASK_GET_PHASE="$task_get_phase" TASK_UPDATE_PHASE="$task_update_phase" TASK_CANCEL_PHASE="$task_cancel_phase" EXPECTED_METHODS="$expected_methods" yq ea '
    select(.kind == "AgentgatewayPolicy" and .metadata.name == strenv(NAME))
    | [ .spec.backend.mcp.guardrails.processors[]
        | select(.methods["tools/call"] == strenv(TOOLS_PHASE)
            and .methods["tasks/get"] == strenv(TASK_GET_PHASE)
            and .methods["tasks/update"] == strenv(TASK_UPDATE_PHASE)
            and .methods["tasks/cancel"] == strenv(TASK_CANCEL_PHASE)
            and (.methods | keys | sort | join(",")) == strenv(EXPECTED_METHODS)
            and .remote.backendRef.name == "mcp-cerbos-shim"
            and .remote.backendRef.namespace == "cerbos"
            and .remote.backendRef.port == 4445
            and .remote.failureMode == "FailClosed") ]
    | length' -)"
  if [[ "$processors" != "1" || "$well_formed" != "1" ]]; then
    echo "ERROR - AgentgatewayPolicy ${name} has a malformed Cerbos guardrail (found" \
         "${processors:-0} processor(s), ${well_formed:-0} well-formed). It must be" \
         "exactly one processor with explicit tools/call, tasks/get, tasks/update, and tasks/cancel phases" \
         "${tools_phase}/${task_get_phase}/${task_update_phase}/${task_cancel_phase} and" \
         "the exact FailClosed shim attachment. Refusing to ship a fail-open MCP backend." >&2
    exit 1
  fi
}

assert_agentgateway_release_locked() {
  local rendered="$1" crd_version chart_version controller_tag data_tag
  crd_version="$(yq '.stages[].actions[] | select(.name == "agentgateway-crds") | .version' stages/stages.yaml)"
  chart_version="$(yq '.stages[].actions[] | select(.name == "agentgateway") | .version' stages/stages.yaml)"
  controller_tag="$(yq '.controller.image.tag' stages/values/agentgateway.yaml)"
  data_tag="$(echo "$rendered" | yq ea 'select(.kind == "AgentgatewayParameters" and .metadata.name == "agentgateway-config") | .spec.image.tag' -)"
  if [[ -z "$crd_version" || "$crd_version" != "$chart_version" || "$crd_version" != "$controller_tag" || "$crd_version" != "$data_tag" ]]; then
    echo "ERROR - agentgateway CRDs/chart/controller/data-plane versions must be identical; got CRDs=${crd_version:-unset}, chart=${chart_version:-unset}, controller=${controller_tag:-unset}, data-plane=${data_tag:-unset}." >&2
    exit 1
  fi
}

assert_promptguard_well_formed() {
  local rendered="$1" rendered_file
  rendered_file="$(mktemp_f)"
  printf '%s\n' "$rendered" > "$rendered_file"
  python3 - "$SECRET_PATTERNS_FILE" "$rendered_file" <<'PY'
import json
import sys

import yaml

registry_path, rendered_path = sys.argv[1:]
with open(registry_path, encoding="utf-8") as handle:
    definitions = json.load(handle)
expected_request = [definition["regex"] for definition in definitions]
expected_response = [
    definition["regex"]
    for definition in definitions
    if definition.get("modelResponse", True) is not False
]
if len(expected_request) != 41 or len(expected_response) != 33:
    raise SystemExit(
        f"unexpected prompt-guard registry counts: request={len(expected_request)} "
        f"response={len(expected_response)}"
    )

with open(rendered_path, encoding="utf-8") as handle:
    resources = list(yaml.safe_load_all(handle))
providers = 0
for resource in resources:
    if not isinstance(resource, dict) or resource.get("kind") != "AgentgatewayBackend":
        continue
    groups = resource.get("spec", {}).get("ai", {}).get("groups", [])
    for group in groups:
        for provider in group.get("providers", []):
            providers += 1
            guard = provider.get("policies", {}).get("ai", {}).get("promptGuard", {})
            request = guard.get("request", [])
            response = guard.get("response", [])
            valid = (
                guard.get("streaming") == "Enabled"
                and len(request) == 1
                and request[0].get("regex", {}).get("action") == "Mask"
                and request[0].get("regex", {}).get("matches") == expected_request
                and len(response) == 1
                and response[0].get("regex", {}).get("action") == "Reject"
                and response[0].get("regex", {}).get("matches") == expected_response
                and response[0].get("rejection") is None
            )
            if not valid:
                raise SystemExit(
                    "AI provider prompt guard does not contain the exact 41 request-mask "
                    "and 33 response-reject patterns"
                )
if providers == 0:
    raise SystemExit("platform render contained no guarded AI providers")
PY
}

assert_promptguard_rejects_empty_response_patterns() {
  local request_only_registry render_log
  request_only_registry="$(mktemp_f)"
  render_log="$(mktemp_f)"
  python3 - "$SECRET_PATTERNS_FILE" "$request_only_registry" <<'PY'
import json
import sys

source, destination = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    definitions = json.load(handle)
for definition in definitions:
    definition["modelResponse"] = False
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(definitions, handle)
PY
  if helm template platform charts/platform -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" \
      --set-file "secretPatterns=$request_only_registry" >"$render_log" 2>&1; then
    echo "ERROR - platform rendered an empty model-response prompt guard instead of failing closed." >&2
    return 1
  fi
  if ! grep -q "empty model-response list" "$render_log"; then
    echo "ERROR - empty model-response prompt-guard render failed for an unexpected reason." >&2
    cat "$render_log" >&2
    return 1
  fi
}

require_tools
mkdir -p "$KUBECONFORM_CACHE"

echo "INFO - Asserting one locked Python environment covers repository tooling"
"$PYTHON" scripts/validate-python-dependencies.py

echo "INFO - Testing serialized Python environment reconciliation"
bash scripts/test-python-env.sh

echo "INFO - Testing vicegerent command working directory"
bash scripts/test-vicegerent-cwd.sh

echo "INFO - Testing that cluster setup defers CNI installation to Helm"
bash scripts/test-cluster-setup-uses-staged-cni.sh

echo "INFO - Asserting every Cerbos policy has a runtime MCP probe"
"$PYTHON" scripts/validate-policy-runtime-coverage.py

echo "INFO - Testing the Kind user-namespace OCI hook repair"
bash scripts/test-kind-userns-hook-access.sh

echo "INFO - Validating the staged-install manifest before rendering local charts"
"$PYTHON" scripts/validate-stages.py --static-only
"$PYTHON" -m unittest scripts.test_validate_stages scripts.test_shim_ci_inputs
"$PYTHON" images/aws-api-mcp-server/test_sitecustomize.py

echo "INFO - Validating YAML syntax"
# Tracked files only: a `find` walk also descends into .worktrees/ and any other
# untracked scratch dir, so an unrelated in-progress branch could fail this run.
# Excluded paths are Helm/Cerbos template sources and generated tool dumps.
git ls-files -z -- '*.yaml' \
  ':(exclude)charts/*/templates/*' \
  ':(exclude)charts/*/policies/*' \
  ':(exclude)charts/*/files/*' \
  ':(exclude)docs/available-mcp-tools/*' \
| while IFS= read -r -d $'\0' file; do
  yq e 'true' "$file" >/dev/null
done

# Each chart's full -f arg list: the committed defaults layer first, then the
# machine values (or the re-rooted first agent) --
# exactly how the installer feeds them. Chart values.yaml files are intentionally
# empty, so both lint and template must supply this layered pair. Parallel indexed
# arrays (CHART_NAMES[i] <-> CHART_ARGS[i]) rather than an associative array, so
# this runs on macOS's stock bash 3.2, which has no `declare -A`.
CHART_NAMES=(platform cerbos-policies mcp-cerbos-shim egress-proxy agent)
CHART_ARGS=(
  "-f $DEFAULTS_VALUES -f $EXAMPLE_VALUES --set-file secretPatterns=$SECRET_PATTERNS_FILE"
  "-f $DEFAULTS_VALUES -f $EXAMPLE_VALUES"
  "-f $DEFAULTS_VALUES -f $EXAMPLE_VALUES"
  "-f $DEFAULTS_VALUES -f $EXAMPLE_VALUES --set-file secretPatterns=$SECRET_PATTERNS_FILE"
  "-f $(agent_defaults_slice) -f $(agent0_slice)"
)

echo "INFO - Linting Helm charts"
for i in "${!CHART_NAMES[@]}"; do
  chart="${CHART_NAMES[$i]}"
  echo "INFO - Linting charts/$chart"
  # shellcheck disable=SC2086  # CHART_ARGS holds intentional -f word splits
  helm lint "charts/$chart" ${CHART_ARGS[$i]}
done

echo "INFO - Rendering charts and validating with kubeconform"
for i in "${!CHART_NAMES[@]}"; do
  chart="${CHART_NAMES[$i]}"
  echo "INFO - Templating + validating charts/$chart"
  retry_cmd bash -c "set -o pipefail; helm template '$chart' 'charts/$chart' ${CHART_ARGS[$i]} | kubeconform ${kubeconform_flags[*]}"
done

if command -v cerbos >/dev/null 2>&1; then
  echo "INFO - Cerbos compile WITH tests (charts/cerbos-policies/test-values.yaml)"
  defs_with="$(mktemp_d)"
  render_cerbos_defs charts/cerbos-policies/test-values.yaml "$defs_with"
  cp charts/cerbos-policies/tests/*.yaml "$defs_with"/
  cerbos compile "$defs_with"

  echo "INFO - Cerbos tests for documented deny-all policy sentinels"
  defs_deny_all="$(mktemp_d)"
  render_cerbos_defs charts/cerbos-policies/deny-all-values.yaml "$defs_deny_all"
  cp charts/cerbos-policies/deny-all-tests/*.yaml "$defs_deny_all"/
  cerbos compile "$defs_deny_all"

  echo "INFO - Cerbos compile-only against $EXAMPLE_VALUES"
  defs_example="$(mktemp_d)"
  render_cerbos_defs "$EXAMPLE_VALUES" "$defs_example"
  cerbos compile --skip-tests "$defs_example"

  echo "INFO - Cerbos tests against the Moveworks DevOps starter"
  cp charts/cerbos-policies/starter-tests/*.yaml "$defs_example"/
  cerbos compile "$defs_example"
elif [[ -n "${CI:-}" ]]; then
  # Locally a missing cerbos is a convenience skip; in CI it would silently turn
  # the only test of the authorization rules into a no-op that still reports green.
  echo "ERROR - cerbos is not installed; the Cerbos policy compile cannot be skipped in CI" >&2
  exit 1
else
  echo "WARN - cerbos not installed; skipping Cerbos policy compile"
fi

echo "INFO - Rendering egress-proxy scrub.py and validating it is valid Python"
helm template egress-proxy charts/egress-proxy -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" \
  --set-file "secretPatterns=$SECRET_PATTERNS_FILE" \
  --show-only templates/addon-configmap.yaml \
  | yq '.data."scrub.py"' \
  | "$PYTHON" -c 'import ast, sys; ast.parse(sys.stdin.read())' \
  || { echo "ERROR - templated egress-proxy scrub.py is not valid Python" >&2; exit 1; }

echo "INFO - Asserting egress-proxy render FAILS without --set-file secretPatterns"
# The addon ConfigMap guards .Values.secretPatterns with `required`, so a bare
# render (no --set-file) must fail. If it ever succeeds, the guard has regressed
# and scrub.py could ship with an empty pattern set.
if helm template egress-proxy charts/egress-proxy -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" \
     --show-only templates/addon-configmap.yaml >/dev/null 2>&1; then
  echo "ERROR - egress-proxy addon-configmap rendered WITHOUT --set-file secretPatterns;" \
       "the required guard is not load-bearing." >&2
  exit 1
fi

echo "INFO - Testing egress-proxy scrub.py secret-redaction patterns"
# scrub.py renders with a single -f, so hand it the merged full values document.
egress_merged="$(mktemp_f)"
yq eval-all 'select(fi==0) * select(fi==1)' "$DEFAULTS_VALUES" "$EXAMPLE_VALUES" > "$egress_merged"
EGRESS_VALUES="$egress_merged" "$PYTHON" scripts/test-scrub-patterns.py

echo "INFO - Asserting platform render FAILS without --set-file secretPatterns"
# platform's promptGuard partial guards .Values.secretPatterns with `required` and
# an empty-list check, so a bare render (no --set-file) must fail. If it ever
# succeeds, the AI-provider leg could ship with an empty secret/PII guard.
if helm template platform charts/platform -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" >/dev/null 2>&1; then
  echo "ERROR - platform rendered WITHOUT --set-file secretPatterns;" \
       "the promptGuard required guard is not load-bearing." >&2
  exit 1
fi

echo "INFO - Asserting the victoria-logs Vector redactor is in sync with secret-patterns.json"
# Leg 4 (Vector VRL) has no render seam like --set-file/embed, so its regexes are
# GENERATED from the canonical JSON and committed; this fails closed on any drift.
"$PYTHON" scripts/gen-vector-redactor.py --check

echo "INFO - Asserting every image we build is deployed on the tag its Makefile defaults to"
"$PYTHON" scripts/validate-image-tags.py

echo "INFO - Asserting values.example.yaml remains a scoped Moveworks DevOps starter"
"$PYTHON" scripts/validate-values-example.py

echo "INFO - Asserting SSH direct egress fails closed and remains FQDN-scoped"
"$PYTHON" scripts/validate-agent-ssh-egress.py

echo "INFO - Asserting every configured model has live pricing"
"$PYTHON" scripts/validate-model-pricing.py

echo "INFO - Validating merged machine-values contracts"
"$PYTHON" scripts/test-validate-machine-values.py
"$PYTHON" scripts/validate-machine-values.py \
  --defaults "$DEFAULTS_VALUES" \
  "$EXAMPLE_VALUES" examples/personal.yaml examples/work.yaml

echo "INFO - Asserting Anthropic and OpenAI model switches stay on Agentgateway"
"$PYTHON" scripts/validate-model-switch-routing.py

echo "INFO - Asserting only vMCP opts into parallel tool calls"
"$PYTHON" scripts/validate-mcp-parallel-tool-calls.py

echo "INFO - Asserting Codex has the baked LSP MCP runtime"
"$PYTHON" scripts/validate-codex-lsp.py

echo "INFO - Asserting Claude Code loads the baked language servers"
"$PYTHON" scripts/validate-claude-lsp.py

echo "INFO - Asserting rendered provider reasoning overrides match configured efforts"
"$PYTHON" images/agent/patches/tests/test_provider_reasoning_overrides.py --chart-dir charts/agent --values values.defaults.yaml

echo "INFO - Asserting harness config ownership reconciliation"
"$PYTHON" scripts/validate-config-reconciliation.py

echo "INFO - Asserting the agent owns ~/.gitconfig instead of seeding it imperatively"
"$PYTHON" scripts/validate-gitconfig-immutable.py

echo "INFO - Asserting agent runtime ownership"
"$PYTHON" scripts/validate-agent-runtime.py

echo "INFO - Testing installer convergence and CSI recovery failure handling"
"$PYTHON" scripts/test_installer_recovery.py

echo "INFO - Testing the interactive agent Bash prompt"
scripts/test-agent-bash-prompt.sh

echo "INFO - Asserting legacy Hermes state migrates into its per-harness home"
"$PYTHON" images/agent/patches/tests/test_hermes_home_migration.py

echo "INFO - Asserting Slack access is single-operator and DM-only"
"$PYTHON" scripts/validate-slack-access.py

echo "INFO - Asserting shared operating guidance reaches every harness"
"$PYTHON" scripts/validate-shared-skill-guidance.py

echo "INFO - Asserting model prompt-guard pattern scope and fixture hygiene"
python3 scripts/validate-promptguard-pattern-scope.py

platform_rendered="$(helm template platform charts/platform -f "$DEFAULTS_VALUES" -f "$EXAMPLE_VALUES" --set-file "secretPatterns=$SECRET_PATTERNS_FILE")"

echo "INFO - Asserting AI prompt guards cover buffered and streaming responses"
assert_promptguard_well_formed "$platform_rendered"

echo "INFO - Asserting an empty model-response pattern set fails closed"
assert_promptguard_rejects_empty_response_patterns

echo "INFO - Asserting the agentgateway release versions stay in lockstep"
assert_agentgateway_release_locked "$platform_rendered"

echo "INFO - Asserting the vMCP Cerbos guardrail is well-formed"
assert_guardrail_well_formed "$platform_rendered"

echo "INFO - Asserting every HTTPRoute pins one Gateway listener via sectionName"
# The Gateway has two listeners: `http` (:80, agent-facing, guardrail phase Full)
# and `internal` (:81, the shim's own re-entrant lookups, phase Request only). A
# parentRef with no sectionName attaches to BOTH, which would expose every route
# on the listener carrying the weaker phase.
unpinned="$(echo "$platform_rendered" | yq ea '
  [ select(.kind == "HTTPRoute")
    | select(.spec.parentRefs[] | has("sectionName") | not)
    | .metadata.name ] | join(" ")' -)"
if [[ -n "$unpinned" ]]; then
  echo "ERROR - HTTPRoute(s) with a parentRef missing sectionName: ${unpinned}." \
       "A parentRef with no sectionName attaches to every Gateway listener," \
       "including the internal one." >&2
  exit 1
fi

echo "INFO - Testing managed Homebrew package reconciliation"
"$PYTHON" -m unittest host.brew.test_generate host.brew.test_reconcile
"$PYTHON" host/brew/reconcile.py validate
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "INFO - Compiling the native macOS notification helper"
  xcrun clang -fobjc-arc -fmodules \
    -fmodules-cache-path="$WORKDIR/notifier-module-cache" \
    -mmacosx-version-min=13.0 host/notifier/main.m \
    -o "$WORKDIR/vicegerent-notifier" \
    -framework Cocoa -framework UserNotifications
fi

echo "INFO - All validations passed"
