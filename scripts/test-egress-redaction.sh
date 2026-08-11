#!/usr/bin/env bash
# test-egress-redaction.sh
# Validate that the egress-proxy scrubs secrets from outbound requests before
# they leave the cluster, by driving real HTTP calls FROM a running agent
# sandbox pod against httpbin.io (a public echo service allowlisted for this
# purpose - see charts/egress-proxy/templates/networkpolicy.yaml and
# addon-configmap.yaml). httpbin echoes back exactly what it received, so a
# response missing the raw secret proves the proxy redacted it before
# forwarding - not just that some client-side masking happened.
#
# All test secrets are fake/synthetic. Only GET/HEAD is exercised, since the
# proxy enforces GET/HEAD-only for external destinations - PEM private-key
# and POST-body scrubbing cannot be exercised this way (see charts/egress-proxy/README.md).
#
# Usage:
#   bash scripts/test-egress-redaction.sh
#
# Any running agent sandbox is picked automatically. Override namespace, agent name,
# pod, or container:
#   AGENT_LABEL=<name> bash scripts/test-egress-redaction.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/cli-ui.sh
source "$SCRIPT_DIR/lib/cli-ui.sh"
# shellcheck source=lib/kube-context.sh
source "$SCRIPT_DIR/lib/kube-context.sh"

NAMESPACE="${NAMESPACE:-agent-sandbox}"
# Operators name their own agents, so match on the label's presence and narrow to one
# name only when AGENT_LABEL is set.
AGENT_LABEL="${AGENT_LABEL:-}"
SELECTOR="vicegerent.io/dashboard${AGENT_LABEL:+=${AGENT_LABEL}}"

command -v kubectl >/dev/null 2>&1 || { ui_error "kubectl is required."; exit 1; }
require_kind_context
CTX=(--context "$KUBE_CONTEXT")

POD="${POD:-$(kubectl "${CTX[@]}" -n "$NAMESPACE" get pods -l "$SELECTOR" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)}"
[[ -n "$POD" ]] || {
  ui_error "No running pod found in namespace '${NAMESPACE}' with label ${SELECTOR}."
  ui_command "kubectl -n ${NAMESPACE} get pods"
  exit 1
}
# Every agent's app container is named 'agent'.
CONTAINER="${CONTAINER:-agent}"
[[ -n "$CONTAINER" ]] || {
  ui_error "Could not resolve the agent container in pod '${POD}'."
  exit 1
}

PASS=0; FAIL=0

pass() { ui_success "$*"; ((PASS++)); }
fail() { ui_error "$*"; ((FAIL++)); }
section() { ui_section "$*"; }

# exec-curl helper
# Runs curl inside the sandbox pod so the request goes through the real
# http_proxy/https_proxy env vars and trusted egress-proxy CA baked into the
# container - not a laptop-side shortcut that would bypass the Cilium policy
# keyed on the agent-sandbox namespace.
DELIM="===EGRESS_TEST_STATUS==="
pod_curl() {
  local url="$1"; shift
  kubectl "${CTX[@]}" -n "$NAMESPACE" exec "$POD" -c "$CONTAINER" -- \
    curl -sS --max-time 15 -o - -w "${DELIM}%{http_code}" "$@" "$url" 2>/dev/null
}

# Splits pod_curl's combined output into BODY and STATUS globals.
BODY=""; STATUS=""
run() {
  local raw; raw="$(pod_curl "$@")"
  STATUS="${raw##*"$DELIM"}"
  BODY="${raw%"$DELIM$STATUS"}"
}

ui_header "Egress proxy redaction test suite"
ui_key_value "Pod" "${NAMESPACE}/${POD}"
ui_key_value "Container" "$CONTAINER"

section "1. httpbin.io reachable through the egress proxy"

ui_info "Probing GET https://httpbin.io/get…"
run "https://httpbin.io/get"
if [[ "$STATUS" == "200" ]]; then
  pass "GET https://httpbin.io/get -> 200 (FQDN allowlist + proxy path OK)"
else
  fail "GET https://httpbin.io/get -> ${STATUS} (expected 200)"
  echo "    body: ${BODY:0:200}"
fi

# Header secret redaction
# httpbin.io/headers echoes back exactly the headers it received, as JSON.

section "2. secrets in request headers are redacted before forwarding"

SLACK_BOT_TOKEN="xoxb-$(printf 'a%.0s' {1..24})"  # pragma: allowlist secret
ui_info "Probing a Slack bot token in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SLACK_BOT_TOKEN}"
if [[ "$BODY" == *"$SLACK_BOT_TOKEN"* ]]; then
  fail "Slack bot token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack bot token (xoxb-...) redacted in custom header"
else
  fail "Slack bot token neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

SLACK_APP_TOKEN="xapp-$(printf 'b%.0s' {1..24})"  # pragma: allowlist secret
ui_info "Probing a Slack app-level token in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SLACK_APP_TOKEN}"
if [[ "$BODY" == *"$SLACK_APP_TOKEN"* ]]; then
  fail "Slack app-level token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack app-level token (xapp-...) redacted in custom header"
else
  fail "Slack app-level token neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BEARER_SECRET="sk-$(printf 'o%.0s' {1..32})"  # pragma: allowlist secret
ui_info "Probing an Authorization bearer-scheme header…"
run "https://httpbin.io/headers" -H "Authorization: Bear""er ${BEARER_SECRET}"
if [[ "$BODY" == *"$BEARER_SECRET"* ]]; then
  fail "Bearer-scheme token reached httpbin unredacted"
elif [[ "$BODY" == *'Bearer <masked>'* ]]; then
  pass "Authorization bearer-scheme token redacted"
else
  fail "Bearer-scheme token neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

BASIC_CREDS="dGVzdHVzZXI6dGVzdHBhc3N3b3Jk" # base64("testuser:testpassword") - fake
ui_info "Probing an Authorization basic-scheme header…"
run "https://httpbin.io/headers" -H "Authorization: Bas""ic ${BASIC_CREDS}"
if [[ "$BODY" == *"$BASIC_CREDS"* ]]; then
  fail "Basic-scheme credentials reached httpbin unredacted"
elif [[ "$BODY" == *'Basic <masked>'* ]]; then
  pass "Authorization basic-scheme credentials redacted"
else
  fail "Basic-scheme credentials neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

API_KEY_SECRET="test-fake-api-key-0000000000000000"  # pragma: allowlist secret
ui_info "Probing an x-api-key header…"
run "https://httpbin.io/headers" -H "x-api-key: ${API_KEY_SECRET}"
if [[ "$BODY" == *"$API_KEY_SECRET"* ]]; then
  fail "x-api-key header reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "x-api-key header redacted"
else
  fail "x-api-key header neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# AWS access key and GitHub token shapes (parity with mcp-cerbos-shim's
# pattern registry). Fixtures are fake and assembled at runtime (no full
# literal token in the source) plus a pragma allowlist, matching the
# fake-fixture convention above.
AWS_KEY="AKIA$(printf 'Q%.0s' $(seq 16))"  # pragma: allowlist secret (fake AWS access key id)
ui_info "Probing an AWS access key ID in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${AWS_KEY}"
if [[ "$BODY" == *"$AWS_KEY"* ]]; then
  fail "AWS access key id reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "AWS access key id (AKIA…) redacted in custom header"
else
  fail "AWS access key id neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

GITHUB_TOKEN="ghp_$(printf 'g%.0s' $(seq 36))"  # pragma: allowlist secret (fake GitHub PAT)
ui_info "Probing a GitHub token in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${GITHUB_TOKEN}"
if [[ "$BODY" == *"$GITHUB_TOKEN"* ]]; then
  fail "GitHub token reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "GitHub token (ghp_…) redacted in custom header"
else
  fail "GitHub token neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# PII shapes: SSN, credit card, US phone. Fake fixtures assembled at runtime.
SSN_FAKE="123""-45-6789"  # pragma: allowlist secret (fake SSN)
ui_info "Probing a US SSN in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${SSN_FAKE}"
if [[ "$BODY" == *"$SSN_FAKE"* ]]; then
  fail "SSN reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "US SSN (NNN-NN-NNNN) redacted in custom header"
else
  fail "SSN neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

VISA_FAKE="4$(printf '1%.0s' $(seq 15))"  # pragma: allowlist secret (fake Visa card)
ui_info "Probing a Visa card number in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${VISA_FAKE}"
if [[ "$BODY" == *"$VISA_FAKE"* ]]; then
  fail "Visa card number reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Visa card number (starts 4, 16 digits) redacted in custom header"
else
  fail "Visa card number neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

PHONE_FAKE="(555) ""123-4567"  # pragma: allowlist secret (fake US phone)
ui_info "Probing a US phone number in a custom header…"
run "https://httpbin.io/headers" -H "X-Test-Secret: ${PHONE_FAKE}"
if [[ "$BODY" == *"$PHONE_FAKE"* ]]; then
  fail "US phone number reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "US phone number ((NNN) NNN-NNNN) redacted in custom header"
else
  fail "US phone number neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# URL path/query redaction
# The proxy scrubs flow.request.path (path + query string) before forwarding,
# so httpbin.io/get's echoed "url"/"args" fields reflect the redacted value.

section "3. secrets in the request URL query string are redacted"

QUERY_TOKEN="xoxb-$(printf 'q%.0s' {1..24})"  # pragma: allowlist secret
ui_info "Probing a Slack token in a query string…"
run "https://httpbin.io/get?token=${QUERY_TOKEN}"
if [[ "$BODY" == *"$QUERY_TOKEN"* ]]; then
  fail "Slack token in query string reached httpbin unredacted"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Slack token in query string redacted before forwarding"
else
  fail "Query-string token neither present nor visibly redacted - unexpected response"
  echo "    body: ${BODY:0:300}"
fi

# Negative controls - enforcement still active on the new host
# Guards against a too-broad allowlist entry accidentally opening up more
# than GET/HEAD, or the FQDN allowlist being effectively a wildcard.

section "4. method + FQDN enforcement unaffected by the httpbin allowlist entry"

ui_info "Probing POST https://httpbin.io/post - expect blocked…"
run "https://httpbin.io/post" -X POST -d 'x=1'
if [[ "$STATUS" == "403" ]]; then
  pass "POST https://httpbin.io/post -> 403 (external POST still blocked)"
else
  fail "POST https://httpbin.io/post -> ${STATUS} (expected 403 - method enforcement regression?)"
  echo "    body: ${BODY:0:200}"
fi

# A non-allowlisted HTTPS host is blocked at the CONNECT stage, so curl has no
# tunnel to report an in-band 403 through and shows 000 instead. Only a 200
# here would indicate an allowlist regression.
ui_info "Probing GET https://example.com/ - expect blocked…"
run "https://example.com/"
if [[ "$STATUS" == "403" || "$STATUS" == "000" ]]; then
  pass "GET https://example.com/ -> ${STATUS} (non-allowlisted FQDN still blocked)"
else
  fail "GET https://example.com/ -> ${STATUS} (expected 403 or 000 - is the allowlist a wildcard?)"
  echo "    body: ${BODY:0:200}"
fi

# git-upload-pack exception is narrow, not a github.com POST bypass
# Guards against the exception widening into "any POST to github.com is fine".

section "5. git-upload-pack exception is narrow (other POSTs to github.com still blocked)"

ui_info "Probing POST https://github.com/ with the wrong path/content type - expect blocked…"
run "https://github.com/" -X POST -d 'x=1'
if [[ "$STATUS" == "403" ]]; then
  pass "POST https://github.com/ -> 403 (git-upload-pack exception does not widen to all POSTs)"
else
  fail "POST https://github.com/ -> ${STATUS} (expected 403 - git-upload-pack exception may be too broad)"
  echo "    body: ${BODY:0:200}"
fi

# Response-body redaction
# Every other test above proves REQUEST-side scrubbing (the secret is in a header
# or URL we send, and the request hook redacts it before httpbin echoes it back).
# To isolate RESPONSE-side scrubbing we need a secret that originates server-side,
# NOT one we send in the clear - anything we send is request-scrubbed first.
#
# Mechanism: send the secret base64-encoded in the URL path to httpbin.io/base64,
# which DECODES it server-side and returns the raw secret in the RESPONSE body.
# The base64 blob matches no request-side pattern, so it passes through untouched;
# the decoded secret then only ever exists in the response, where the response()
# hook must scrub it.
#
# Caveat: this depends on httpbin.io/base64 returning a text/* or
# application/json Content-Type - the response scrubber deliberately skips
# binary bodies, so a non-text response would fail this test on content-type
# grounds, not a real redaction regression.

section "6. secrets in the RESPONSE body are redacted (echo-attack guard)"

NOTION_TOKEN="ntn_$(printf 't%.0s' $(seq 24))"  # pragma: allowlist secret (fake Notion token)
# Keep base64 padding: go-httpbin's /base64 decode() tries URLEncoding then
# StdEncoding, both PADDED - stripping '=' triggers "illegal base64 data".
NOTION_B64="$(printf '%s' "$NOTION_TOKEN" | base64 | tr '+/' '-_')"
ui_info "Probing a server-originated secret via httpbin.io/base64 decode…"
run "https://httpbin.io/base64/${NOTION_B64}"
if [[ "$BODY" == *"$NOTION_TOKEN"* ]]; then
  fail "Notion token survived in the response body - response-side scrubbing not applied (or /base64 served a non-text Content-Type)"
  echo "    body: ${BODY:0:300}"
elif [[ "$BODY" == *'<masked>'* ]]; then
  pass "Notion token in the decoded response body redacted before reaching the sandbox"
else
  fail "Response body neither carried the token nor a <masked> marker - unexpected /base64 response"
  echo "    status: ${STATUS} body: ${BODY:0:300}"
fi

ui_section "Summary"
ui_key_value "Passed" "$PASS"
ui_key_value "Failed" "$FAIL"

if [[ $FAIL -gt 0 ]]; then
  echo "Diagnostics:"
  echo "  kubectl --context ${KUBE_CONTEXT} logs -n egress-proxy deploy/egress-proxy --tail=50"
  echo "  kubectl --context ${KUBE_CONTEXT} -n egress-proxy get ciliumnetworkpolicy egress-proxy -o yaml"
  echo "  kubectl --context ${KUBE_CONTEXT} -n egress-proxy get configmap egress-proxy-addon -o yaml"
fi

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
