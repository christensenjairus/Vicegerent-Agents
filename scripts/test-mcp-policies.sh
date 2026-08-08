#!/usr/bin/env bash
# test-mcp-policies.sh
# Validate that agentgateway + Cerbos policies are correctly enforced at runtime.
#
# All backends are aggregated behind the single ToolHive vMCP (/mcp/vmcp); tools
#   surface prefixed by workload (e.g. kubernetes_resources_get) with no allowlist.
#   With the vMCP tool-discovery optimizer on (thv vmcp serve --optimizer, the
#   default), tools/list exposes only two meta-tools — find_tool (search) and
#   call_tool (invoke by name) — so real tools are discovered via find_tool and
#   invoked through call_tool{tool_name, parameters}, which mcp-cerbos-shim unwraps
#   before its Cerbos lookup. This suite detects the optimizer and probes that same
#   path so the guardrail is exercised exactly as it is in production.
#   Every Cerbos resource policy is represented below. Backends absent from the
#   live vMCP are reported as skipped; enabled backends are exercised through
#   safe calls that must be denied before the backend can read or mutate data.
#
# Usage (port-forward in another terminal first):
#   kubectl -n agentgateway-system port-forward svc/agentgateway-proxy 8080:80
#   bash scripts/test-mcp-policies.sh
#
# Override the gateway URL or unauthenticated placeholder API key:
#   GATEWAY_URL=http://localhost:8080 MY_KEY=agent bash scripts/test-mcp-policies.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/cli-ui.sh
source "$SCRIPT_DIR/lib/cli-ui.sh"
# shellcheck source=lib/kube-context.sh
source "$SCRIPT_DIR/lib/kube-context.sh"

# kubectl is optional here (the suite mainly drives the gateway URL); resolve the
# context only when kubectl is present, since a few checks shell out to it.
if command -v kubectl >/dev/null 2>&1; then
  require_kind_context
fi

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
API_KEY="${MY_KEY:-agent}"
# Random secret name so the test is self-describing in k8s audit logs. `od`
# reads a fixed byte count; avoid `tr | head`, which SIGPIPEs under pipefail
# and can silently truncate the value.
SECRET_NAME="policy-test-$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"  # pragma: allowlist secret
[[ "$SECRET_NAME" != "policy-test-" ]] || SECRET_NAME="policy-test-xxxxxxxx"  # pragma: allowlist secret

PASS=0; FAIL=0; SKIP=0
# Set to 1 in section 1 when the vMCP optimizer (find_tool/call_tool) is detected.
OPTIMIZER=0

pass() { ui_success "$*"; ((PASS++)); }
fail() { ui_error "$*"; ((FAIL++)); }
skip() { ui_warn "$*"; ((SKIP++)); }
section() { ui_section "$*"; }

# MCP session helpers

SESSION_ID=""
mcp_post() {
  local url="$1" payload="$2" session="${3:-}"
  local hdr; hdr=$(mktemp)
  local extra=(); [[ -n "$session" ]] && extra=(-H "Mcp-Session-Id: $session")
  local body
  body=$(curl -sf --max-time 20 \
    -D "$hdr" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    "${extra[@]+"${extra[@]}"}" \
    -X POST "$url" -d "$payload" 2>/dev/null) || true
  SESSION_ID=$(grep -i '^mcp-session-id:' "$hdr" | awk '{print $2}' | tr -d '\r' || true)
  rm -f "$hdr"
  printf '%s' "$body"
}

# Open a session to an MCP endpoint. Prints the session ID and sets SESSION_ID.
open_session() {
  local url="$1"
  local init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"policy-test","version":"1"}}}'
  SESSION_ID=""
  mcp_post "$url" "$init" >/dev/null
  if [[ -z "$SESSION_ID" ]]; then
    ui_error "Could not open MCP session to ${url}."
    exit 1
  fi
}

# Fetch the tool list for the current SESSION_ID. Prints names one per line.
get_tools() {
  local url="$1"
  local list='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
  local resp; resp=$(mcp_post "$url" "$list" "$SESSION_ID")
  python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split('\n') if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    for t in d.get('result', {}).get('tools', []):
        print(t['name'])
except Exception as e:
    sys.stderr.write(f'parse error: {e}\n')
" <<< "$resp"
}

# Query the vMCP optimizer's find_tool and print discoverable tool names, one per
# line. keyword drives the ranked FTS5 search (underscore tool names don't tokenize
# well as keywords — use the backend name); description adds semantic context.
find_tool_names() {
  local url="$1" description="$2" keyword="$3"
  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'find_tool','arguments':{'tool_description':sys.argv[1],'tool_keywords':[sys.argv[2]]}}}))
" "$description" "$keyword")
  local resp; resp=$(mcp_post "$url" "$payload" "$SESSION_ID")
  echo "$resp" | python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split('\n') if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    inner = json.loads(d['result']['content'][0]['text'])
    for t in (inner.get('tools') or []):
        print(t['name'])
except Exception:
    pass
"
}

# Build a tools/call payload for a real backend tool. When OPTIMIZER=1 the tool is
# invoked through the vMCP optimizer's call_tool meta-tool ({tool_name, parameters}),
# which mcp-cerbos-shim unwraps before its Cerbos lookup — the same path an agent
# takes. When off, the tool is called directly by name.
_tool_call_payload() {
  local tool="$1" args_json="$2"
  OPTIMIZER="$OPTIMIZER" python3 -c "
import json, os, sys
tool, args = sys.argv[1], json.loads(sys.argv[2])
if os.environ.get('OPTIMIZER') == '1':
    params = {'name': 'call_tool', 'arguments': {'tool_name': tool, 'parameters': args}}
else:
    params = {'name': tool, 'arguments': args}
print(json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': params}))
" "$tool" "$args_json"
}

# Call a tool. Prints raw SSE/JSON response.
call_tool() {
  local url="$1" tool="$2" args_json="$3"
  mcp_post "$url" "$(_tool_call_payload "$tool" "$args_json")" "$SESSION_ID"
}

# Parse "is this a Cerbos-denied response?" from a tools/call SSE response.
# Looks for the error text the shim returns.
# is_cerbos_denied: 'denied' only when agentgateway/Cerbos blocked the call.
# 'allowed' when Cerbos passed the call through (even if k8s then errored).
#
# Cerbos denials come back as JSON-RPC error code -32001 with the message
# "Access denied by security policy...". k8s-level errors (tool/config failures)
# use -32603. We key on the error code — it's stable and unambiguous.
is_cerbos_denied() {
  local resp="$1"
  echo "$resp" | python3 -c "
import sys, json
raw = sys.stdin.read()
lines = [l[5:].strip() for l in raw.split('\n') if l.startswith('data:')]
body = lines[0] if lines else raw
try:
    d = json.loads(body)
    err = d.get('error', {})
    code = err.get('code')
    msg  = str(err.get('message', ''))
    # -32001 is the agentgateway/Cerbos policy-denial code.
    # Also catch the human-readable phrase as a belt-and-suspenders fallback.
    if code == -32001 or 'Access denied by security policy' in msg:
        print('denied')
    else:
        print('allowed')
except Exception:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# Return the denial text (or the backend result text) from a tools/call response.
response_text() {
  local resp="$1"
  python3 -c '
import json, sys
raw = sys.stdin.read()
events = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
body = events[0] if events else raw
try:
    data = json.loads(body)
    if data.get("error"):
        print(data["error"].get("message", ""))
    else:
        print(" ".join(str(item.get("text", "")) for item in data.get("result", {}).get("content", [])))
except Exception:
    print(raw)
' <<<"$resp"
}

# A policy probe is intentionally malformed or out of scope, so a correct
# guardrail rejects it before the backend can perform a read or write. The
# expected fragment ties the result to the specific Cerbos rule instead of
# accepting an unrelated schema, lookup, or upstream failure as coverage.
deny_probe() {
  local policy="$1" tool="$2" args="$3" expected="$4"
  local available
  if [[ "$OPTIMIZER" -eq 1 ]]; then
    available=$(find_tool_names "$VMCP_URL" "$tool" "${tool%%_*}")
  else
    available="$TOOLS"
  fi
  if ! grep -qx "$tool" <<<"$available"; then
    skip "$policy — $tool is not enabled"
    return
  fi

  local resp verdict text
  resp=$(call_tool "$VMCP_URL" "$tool" "$args")
  verdict=$(is_cerbos_denied "$resp")
  text=$(response_text "$resp")
  if [[ "$verdict" == "denied" && "$text" == *"$expected"* ]]; then
    pass "$policy → denied by its Cerbos rule"
  elif [[ "$verdict" == "denied" ]]; then
    fail "$policy → denied for the wrong reason: ${text:0:220}"
  else
    fail "$policy → was not denied by Cerbos: ${text:0:220}"
  fi
}

# The first argument is consumed only by the static coverage validator. Keeping
# it on the executable probe call makes coverage drift harder than maintaining a
# disconnected checklist beside the tests.
policy_probe() {
  local resource="$1"
  shift
  deny_probe "$@"
  : "$resource"
}

ui_header "MCP policy enforcement test suite"
ui_key_value "Gateway" "$GATEWAY_URL"
ui_key_value "Secret probe" "$SECRET_NAME"

VMCP_URL="${GATEWAY_URL}/mcp/vmcp"

section "1. vMCP tool surface — aggregated at the gateway"

open_session "$VMCP_URL"
TOOLS=$(get_tools "$VMCP_URL")

# Tool selection is done in the vMCP (aggregation.tools, per backend); agentgateway
# adds no per-tool allowlist in this setup (it could in a centralized one). Just
# confirm the vMCP is exposing tools.
TOOL_COUNT=$(echo "$TOOLS" | grep -c . || true)
if [[ "$TOOL_COUNT" -gt 0 ]]; then
  pass "vMCP exposes ${TOOL_COUNT} tools at the surface"
else
  fail "vMCP exposed no tools — backends down or vMCP not aggregating?"
fi

# With the optimizer on, tools/list carries only find_tool + call_tool; the real
# tools are behind find_tool. Detect that and switch section 2's probes to the
# call_tool path (set OPTIMIZER=1). Otherwise tools surface raw ({workload}_<tool>).
if echo "$TOOLS" | grep -qx "call_tool" && echo "$TOOLS" | grep -qx "find_tool"; then
  OPTIMIZER=1
  pass "vMCP optimizer on — probing tools via find_tool / call_tool"
fi

# Section 2's Cerbos Secret block depends on these exact kubernetes tool names, so
# confirm they exist: discoverable via find_tool (optimizer) or listed raw.
if [[ "$OPTIMIZER" -eq 1 ]]; then
  DISCOVERED=$(find_tool_names "$VMCP_URL" "get and list kubernetes resources" "kubernetes")
fi
for must_have in "kubernetes_resources_get" "kubernetes_resources_list"; do
  if [[ "$OPTIMIZER" -eq 1 ]]; then
    if echo "$DISCOVERED" | grep -qx "$must_have"; then
      pass "tool discoverable via find_tool: ${must_have}"
    else
      skip "tool not enabled: ${must_have}"
    fi
  elif echo "$TOOLS" | grep -qx "$must_have"; then
    pass "tool present: ${must_have}"
  else
    skip "tool not enabled: ${must_have}"
  fi
done

# Cerbos Secret block
# All probes are READ-ONLY. Secret probes use a randomly generated name that
# almost certainly does not exist — even if policy fails, there is nothing to
# return. The non-secret control probe uses a namespace that certainly exists
# but we ask for a resource name that won't exist either, so Cerbos is tested
# without leaking real cluster state if a policy is mis-configured.

section "2. Cerbos guardrail — Secret reads must be denied"

open_session "$VMCP_URL"

if [[ "$OPTIMIZER" -eq 1 ]]; then
  KUBE_GET_ENABLED=$(grep -c '^kubernetes_resources_get$' <<<"$DISCOVERED" || true)
  KUBE_LIST_ENABLED=$(grep -c '^kubernetes_resources_list$' <<<"$DISCOVERED" || true)
else
  KUBE_GET_ENABLED=$(grep -c '^kubernetes_resources_get$' <<<"$TOOLS" || true)
  KUBE_LIST_ENABLED=$(grep -c '^kubernetes_resources_list$' <<<"$TOOLS" || true)
fi

# 2a: resources_get on a Secret — must be denied before k8s is ever contacted.
# Args: apiVersion + kind (kubernetes-mcp-server format). No context arg needed.
# The secret name is random and almost certainly absent; Cerbos denies before k8s lookup.
if [[ "$KUBE_GET_ENABLED" -eq 0 ]]; then
  skip "Kubernetes get Secret policy — kubernetes_resources_get is not enabled"
else
  ui_info "Probing kubernetes_resources_get(Secret/${SECRET_NAME})…"
  RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_get" \
    "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"name\":\"${SECRET_NAME}\",\"namespace\":\"default\"}")
  VERDICT=$(is_cerbos_denied "$RESP")
  if [[ "$VERDICT" == "denied" ]]; then
    pass "kubernetes_resources_get(Secret) → denied by Cerbos"
  elif [[ "$VERDICT" == "allowed" ]]; then
    fail "kubernetes_resources_get(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
  else
    fail "kubernetes_resources_get(Secret) → unknown response"
    echo "    raw: ${RESP:0:300}"
  fi
fi

# 2b: resources_list of Secrets — must be denied.
# Namespace is intentionally a fake one so even if policy fails, no secrets are returned.
if [[ "$KUBE_LIST_ENABLED" -eq 0 ]]; then
  skip "Kubernetes list Secret policy — kubernetes_resources_list is not enabled"
else
  ui_info "Probing kubernetes_resources_list(kind=Secret, ns=policy-test-ns)…"
  RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_list" \
    "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"namespace\":\"policy-test-nonexistent-ns\"}")
  VERDICT=$(is_cerbos_denied "$RESP")
  if [[ "$VERDICT" == "denied" ]]; then
    pass "kubernetes_resources_list(Secret) → denied by Cerbos"
  elif [[ "$VERDICT" == "allowed" ]]; then
    fail "kubernetes_resources_list(Secret) → ALLOWED — Cerbos guardrail not enforcing!"
  else
    fail "kubernetes_resources_list(Secret) → unknown response"
    echo "    raw: ${RESP:0:300}"
  fi
fi

# 2c: resources_get on a ConfigMap — must NOT be denied (Cerbos should not over-block).
# A k8s-level 404 is fine — it means Cerbos passed it through (correct behaviour).
if [[ "$KUBE_GET_ENABLED" -ne 0 ]]; then
  ui_info "Probing kubernetes_resources_get(ConfigMap/policy-test-nonexistent) — expect allowed…"
  RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_get" \
    "{\"apiVersion\":\"v1\",\"kind\":\"ConfigMap\",\"name\":\"policy-test-nonexistent\",\"namespace\":\"default\"}")
  VERDICT=$(is_cerbos_denied "$RESP")
  if [[ "$VERDICT" == "denied" ]]; then
    fail "kubernetes_resources_get(ConfigMap) → DENIED — Cerbos is over-blocking non-secrets!"
  else
    pass "kubernetes_resources_get(ConfigMap) → passed Cerbos (k8s-level result is irrelevant)"
  fi
fi

# 2d: resources_list for Pods — must NOT be denied (non-secret, non-empty kind).
if [[ "$KUBE_LIST_ENABLED" -ne 0 ]]; then
  ui_info "Probing kubernetes_resources_list(kind=Pod) — expect allowed…"
  RESP=$(call_tool "$VMCP_URL" "kubernetes_resources_list" \
    "{\"apiVersion\":\"v1\",\"kind\":\"Pod\",\"namespace\":\"default\"}")
  VERDICT=$(is_cerbos_denied "$RESP")
  if [[ "$VERDICT" == "denied" ]]; then
    fail "kubernetes_resources_list(Pod) → DENIED — Cerbos is over-blocking non-secrets!"
  else
    pass "kubernetes_resources_list(Pod) → passed Cerbos (correct)"
  fi
fi

# Complete enabled-policy coverage
# These calls are all guaranteed denials. They use nonexistent/out-of-scope
# identifiers, empty target sets, internal-only URLs, or values above a hard
# cap. A regression therefore cannot create, update, delete, acknowledge, or
# otherwise mutate a backend resource.

section "3. Cerbos + shim policies — safe denial probes"

policy_probe "k8s_resource" "Kubernetes missing kind" "kubernetes_resources_get" \
  '{"apiVersion":"v1","name":"policy-test-never-exists","namespace":"default"}' \
  "Could not resolve a Kubernetes resource kind"
policy_probe "gitlab_project" "GitLab project allowlist" "gitlab_get_project" \
  '{"project_id":"999999999"}' \
  "outside the allowed project list"
policy_probe "github_repo" "GitHub repository allowlist" "github_issue_read" \
  '{"owner":"policy-test-never-allowed","repo":"policy-test-never-allowed","issue_number":1,"method":"get"}' \
  "outside the allowed repo list"
policy_probe "aws_command" "AWS unparseable command" "aws_call_aws" \
  '{"cli_command":"aws"}' "could not be parsed"
policy_probe "aws_command" "AWS credential minting" "aws_call_aws" \
  '{"cli_command":"aws eks get-token --cluster-name policy-test-never-exists"}' \
  "not allowed to run AWS operations that mint"
policy_probe "aws_command" "AWS secret value reads" "aws_call_aws" \
  '{"cli_command":"aws secretsmanager get-secret-value --secret-id policy-test-never-exists"}' \
  "not allowed to read secret values"
policy_probe "jira_project" "Jira project scope" "jira_jira_create_issue" \
  '{"project_key":"POLICYTESTNEVERALLOWED","summary":"policy test — must be denied","issue_type":"PolicyTest","assignee":"policy-test@example.invalid"}' \
  "only create/update Jira issues in its allowed projects"
policy_probe "linear_team" "Linear team scope" "linear_save_issue" \
  '{"team":"policy-test-never-allowed","title":"policy test — must be denied","assignee":"policy-test@example.invalid"}' \
  "only create/update Linear issues for the DEVOPS team"
policy_probe "alertmanager_silence" "Alertmanager silence duration" "alertmanager_createSilence" \
  '{"alertName":"PolicyTestNeverFires","duration":"1000000h","comment":"policy test — must be denied","matchers":[]}' \
  "Silence duration exceeds"
policy_probe "alertmanager_alert_query" "Alertmanager query filter" "alertmanager_getAlerts" '{}' \
  "getAlerts requires filterLabel"
policy_probe "pagerduty_incident" "PagerDuty incident mutation" "pagerduty_manage_incidents" \
  '{"manage_request":{"incident_ids":[],"status":"triggered"}}' \
  "only acknowledge or resolve PagerDuty incidents"
policy_probe "notion_page" "Notion create parent" "notion_notion-create-pages" \
  '{"parent":{"page_id":"00000000000000000000000000000000"},"pages":[]}' \
  "New Notion pages may only be created"
policy_probe "firecrawl_session" "Firecrawl raw code" "firecrawl_firecrawl_interact" \
  '{"url":"https://example.com","code":"return 1"}' \
  "may not pass raw code"
policy_probe "web_crawl" "Web crawl internal target" "tavily_tavily_crawl" \
  '{"url":"http://127.0.0.1/policy-test"}' \
  "may not crawl/map/fetch/monitor an internal-only host"
policy_probe "web_crawl" "Web crawl link cap" "tavily_tavily_crawl" \
  '{"url":"https://example.com","limit":2147483647}' \
  "Crawl/map limit exceeds"
policy_probe "web_crawl" "Web crawl depth cap" "tavily_tavily_crawl" \
  '{"url":"https://example.com","max_depth":2147483647}' \
  "Crawl/map depth exceeds"
policy_probe "web_crawl" "Web crawl breadth cap" "tavily_tavily_map" \
  '{"url":"https://example.com","max_breadth":2147483647}' \
  "Crawl/map breadth exceeds"

# Grafana and Elastic deny lists are deliberately machine-configurable. Prefer
# values.yaml when present; repository authors who keep machine profiles only
# as examples use examples/work.yaml as the deterministic policy-test fallback.
if [[ -f "$SCRIPT_DIR/../values.yaml" ]]; then
  POLICY_VALUES="$SCRIPT_DIR/../values.yaml"
else
  POLICY_VALUES="$SCRIPT_DIR/../examples/work.yaml"
fi
if [[ -f "$POLICY_VALUES" ]] && command -v yq >/dev/null 2>&1; then
  GRAFANA_DENIED=$(yq -r '.policy.dataAccess.grafana.deniedDatasourceUids[0] // .policy.dataAccess.grafana.deniedDatasourceNames[0] // ""' "$POLICY_VALUES")
  ELASTIC_DENIED=$(yq -r '.policy.dataAccess.elastic.deniedIndexPatterns[0] // ""' "$POLICY_VALUES")
else
  GRAFANA_DENIED=""
  ELASTIC_DENIED=""
fi
if [[ -n "$GRAFANA_DENIED" ]]; then
  policy_probe "grafana_datasource" "Grafana datasource denylist" "grafana_get_datasource" \
    "{\"uid\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$GRAFANA_DENIED")}" \
    "not allowed to query this Grafana datasource"
else
  skip "Grafana datasource denylist — no denied datasource is configured in ${POLICY_VALUES##*/}"
fi
if [[ -n "$ELASTIC_DENIED" ]]; then
  policy_probe "elastic" "Elastic index denylist" "elastic_platform_core_search" \
    "{\"index\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$ELASTIC_DENIED")}" \
    "not allowed to access that Elasticsearch data"
else
  skip "Elastic index denylist — no denied index pattern is configured in ${POLICY_VALUES##*/}"
fi

# 4: Guardrail attachment check — verify the policy carries the shim attachment.
# A missing guardrail silently fails open (FailClosed only covers shim failures, not absence).
ui_info "Verifying guardrail attachment to the vmcp-mcp-tools policy…"
if command -v kubectl &>/dev/null; then
  GUARDRAIL=$(kubectl --context "$KUBE_CONTEXT" -n agentgateway-system get agentgatewaypolicy vmcp-mcp-tools \
    -o jsonpath='{.spec.backend.mcp.guardrails.processors[0].remote.backendRef.name}' 2>/dev/null || true)
  if [[ "$GUARDRAIL" == "mcp-cerbos-shim" ]]; then
    pass "guardrail attached: mcp-cerbos-shim (FailClosed)"
  elif [[ -z "$GUARDRAIL" ]]; then
    fail "guardrail NOT attached to vmcp-mcp-tools — Secret block silently fails open!"
  else
    fail "guardrail attached to unexpected backend: ${GUARDRAIL}"
  fi
else
  ui_warn "kubectl is not available — skipping the live guardrail attachment check."
fi

ui_section "Summary"
ui_key_value "Passed" "$PASS"
ui_key_value "Failed" "$FAIL"
ui_key_value "Skipped" "$SKIP"

if [[ $FAIL -gt 0 ]]; then
  echo "Diagnostics:"
  echo "  kubectl -n agentgateway-system get agentgatewaypolicies -o yaml"
  echo "  kubectl -n agentgateway-system logs deploy/agentgateway-proxy --tail=50 | grep -i 'cerbos\|guardrail\|deny'"
  echo "  kubectl -n cerbos logs deploy/cerbos --tail=30"
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
