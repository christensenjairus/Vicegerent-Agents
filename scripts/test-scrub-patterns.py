#!/usr/bin/env python3
"""Unit-test the egress-proxy scrub.py secret-redaction regex registry.

scrub.py is embedded as a ConfigMap block-scalar inside a Helm template
(charts/egress-proxy/templates/addon-configmap.yaml), not a normal importable
Python module, so this test renders the ConfigMap with `helm template` (same path
scripts/validate.sh uses, including --set-file secretPatterns=…), stubs the
`mitmproxy` import, exec()s the rendered source, and asserts against the REAL
compiled REDACT_PATTERNS — not a hand-copied duplicate that could silently drift
from what ships. Those patterns are injected from the single canonical source
images/mcp-cerbos-shim/internal/server/secret-patterns.json (the same file the Go
shim embeds via //go:embed), so this exercises exactly the shapes both runtimes match.

Fake/synthetic fixtures only, built by concatenation so no literal credential
string sits verbatim in this file (keeps detect-secrets from flagging the test).

Usage:
  python3 scripts/test-scrub-patterns.py
  SCRUB_PY=/path/to/rendered_scrub.py python3 scripts/test-scrub-patterns.py
"""
import json
import os
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(REPO, "charts", "egress-proxy")
EXAMPLE_VALUES = os.path.join(REPO, "values.example.yaml")
EGRESS_VALUES = os.environ.get("EGRESS_VALUES")
SECRET_PATTERNS = os.path.join(
    REPO, "images", "mcp-cerbos-shim", "internal", "server", "secret-patterns.json")

R = "<masked>"


def _need(tool):
    from shutil import which
    if which(tool) is None:
        print(f"SKIP - {tool} not installed; cannot render scrub.py", file=sys.stderr)
        sys.exit(0)


def render_scrub_py():
    """helm-template the egress-proxy ConfigMap against the machine-plane egress
    slice and return the scrub.py source string. The chart reads its values
    directly now (no Flux ${var} substitution): EGRESS_VALUES is the already
    re-rooted `.egress` subtree; when unset we slice it from values.example.yaml."""
    _need("helm")
    _need("yq")

    values = EGRESS_VALUES
    tmp = None
    if not values:
        egress = subprocess.check_output(["yq", ".egress", EXAMPLE_VALUES])
        with tempfile.NamedTemporaryFile("wb", suffix=".yaml", delete=False) as f:
            f.write(egress)
            tmp = f.name
        values = tmp
    try:
        rendered = subprocess.check_output(
            ["helm", "template", "egress-proxy", CHART, "-f", values,
             "--set-file", "secretPatterns=" + SECRET_PATTERNS,
             "--show-only", "templates/addon-configmap.yaml"]
        )
    finally:
        if tmp:
            os.unlink(tmp)
    scrub = subprocess.run(
        ["yq", '.data."scrub.py"'], input=rendered, stdout=subprocess.PIPE, check=True
    ).stdout.decode()
    return scrub


def load_scrub(source):
    """exec the scrub.py source with mitmproxy stubbed; return its namespace."""
    mitm = types.ModuleType("mitmproxy")
    http = types.ModuleType("mitmproxy.http")
    http.HTTPFlow = object
    http.Response = object
    mitm.http = http
    sys.modules["mitmproxy"] = mitm
    sys.modules["mitmproxy.http"] = http
    ns = {}
    exec(compile(source, "scrub.py", "exec"), ns)
    return ns


# Fake, secret-SHAPED fixtures (built by concatenation). pragma: allowlist secret
def fixtures():
    return [
        ("ssh_private_key",
         "-----BEGIN " + "OPENSSH " + "PRIVATE" + " KEY-----\n"  # pragma: allowlist secret
         + "b3BlbnNzaC1rZXktdjEAAAAA" + "\n-----END " + "OPENSSH " + "PRIVATE" + " KEY-----"),
        ("slack_bot", "xox" + "b-" + "1" * 10 + "-" + "2" * 10 + "-" + "a" * 24),        # pragma: allowlist secret
        ("slack_app", "xapp-" + "1-" + "A" * 10 + "-" + "9" * 20),                       # pragma: allowlist secret
        ("bearer", "Bear" + "er " + "z" * 20 + "." + "y" * 20 + "." + "x" * 10),         # pragma: allowlist secret
        ("basic", "Bas" + "ic " + "b" * 24 + "=="),                                       # pragma: allowlist secret
        ("aws", "AKIA" + "Q" * 16),                                                       # pragma: allowlist secret
        ("github", "gh" + "p_" + "g" * 36),                                               # pragma: allowlist secret
        ("gitlab", "glp" + "at-" + "l" * 20),                                             # pragma: allowlist secret
        ("google", "AIza" + "G" * 35),                                                    # pragma: allowlist secret
        ("openai", "sk-" + "o" * 20),                                                     # pragma: allowlist secret
        ("openai_proj", "sk-" + "proj-" + "p" * 20),                                      # pragma: allowlist secret
        ("anthropic", "sk-" + "ant-" + "n" * 20),                                         # pragma: allowlist secret
        ("stripe", "sk" + "_live_" + "s" * 16),                                           # pragma: allowlist secret
        ("notion", "ntn" + "_" + "t" * 20),                                               # pragma: allowlist secret
        ("twilio", "SK" + "f" * 32),                                                      # pragma: allowlist secret
        ("npm", "npm" + "_" + "m" * 36),                                                  # pragma: allowlist secret
        ("jwt", "eyJ" + "h" * 10 + "." + "eyJ" + "p" * 10 + "." + "s" * 10),              # pragma: allowlist secret
        # PII (fake) — SSN, two card issuer shapes, and a US phone number.
        ("ssn", "123" + "-" + "45" + "-" + "6789"),
        ("cc_visa", "4" + "1" * 15),                       # 16-digit Visa (starts 4)
        ("cc_mastercard", "5" + "1" + "0" * 14),           # 16-digit Mastercard (starts 51)
        ("cc_amex", "3" + "4" + "0" * 13),                 # 15-digit Amex (starts 34)
        ("cc_discover", "6011" + "0" * 12),                # 16-digit Discover (starts 6011)
        ("phone", "(" + "555" + ") " + "123" + "-" + "4567"),
    ]


# Fake flow stand-ins for _mcp_tool_calls/_mcp_response_entries/_mcp_tool_responses --
# NOT a real mitmproxy HTTPFlow, just the narrow surface those functions touch
# (.request/.response .method/.headers/.content/.get_text()).
class _FakeHeaders(dict):
    def get(self, key, default=""):
        return dict.get(self, key, default)


class _FakeMessage:
    def __init__(self, method="POST", headers=None, text="", content=b"x"):
        self.method = method
        self.headers = _FakeHeaders(headers or {})
        self.content = content
        self._text = text

    def get_text(self, strict=False):
        return self._text


class _FakeFlow:
    def __init__(self, request=None, response=None):
        self.request = request
        self.response = response


def mcp_flow(request_body, response_text=None, response_content_type="application/json"):
    """Build a _FakeFlow carrying a JSON-RPC request_body (dict or list) and,
    optionally, a raw response_text served with response_content_type."""
    req = _FakeMessage(
        method="POST",
        headers={"content-type": "application/json"},
        text=json.dumps(request_body),
    )
    resp = None
    if response_text is not None:
        resp = _FakeMessage(
            headers={"content-type": response_content_type},
            text=response_text,
            content=b"x",
        )
    return _FakeFlow(request=req, response=resp)


def apply_regex(patterns, text):
    # patterns is REDACT_PATTERNS: a list of (name, compiled_pattern) tuples.
    total = 0
    for _name, pat in patterns:
        text, n = pat.subn(R, text)
        total += n
    return text, total


def main():
    source = os.environ.get("SCRUB_PY")
    source = open(source).read() if source else render_scrub_py()
    ns = load_scrub(source)

    patterns = ns["REDACT_PATTERNS"]
    failures = 0

    # 1. Every fixture must be caught by the regex registry.
    for name, secret in fixtures():
        out, n = apply_regex(patterns, "prefix " + secret + " suffix")
        if n == 0 or secret in out:
            print(f"  FAIL {name}: not redacted -> {out!r}")
            failures += 1
        else:
            print(f"  ok   {name}")

    # 2. Ordinary text must be left untouched (no over-redaction).
    for clean in ("This PR closes the auth bug, no credentials involved.",
                  "GET /api/v1/users?page=2&sort=name",
                  "temperature 0.7, max_tokens 4096"):
        _, n = apply_regex(patterns, clean)
        if n != 0:
            print(f"  FAIL clean text over-redacted ({n}): {clean!r}")
            failures += 1
    print("  ok   clean text untouched" if failures == 0 else "")

    # 2b. PII prefix/range scoping must NARROW matches — these must NOT be caught,
    #     proving the card patterns are issuer-prefix-scoped (not a naive 13-19
    #     digit catch-all) and the SSN pattern excludes the invalid ranges.
    pii_negatives = [
        ("16-digit number, no known issuer prefix", "9" * 16),
        ("16-digit number starting 1 (unassigned IIN)", "1234567890123456"),
        ("SSN with invalid area 000", "000" + "-" + "12" + "-" + "3456"),
        ("SSN with invalid serial 0000", "123" + "-" + "45" + "-" + "0000"),
        ("bare 10-digit run (no phone separators)", "5551234567"),
    ]
    for label, text in pii_negatives:
        _, n = apply_regex(patterns, "value " + text + " end")
        if n != 0:
            print(f"  FAIL PII over-match ({n}): {label}: {text!r}")
            failures += 1
        else:
            print(f"  ok   PII scoped-out: {label}")

    # 3. _redact runs the single regex layer and returns (text, count, breakdown).
    #    Assert a known secret is redacted, counted, and named in the breakdown.
    #    scrub.py has no second (gitleaks) layer and no network hop anymore, so there
    #    is no fail-open contract left to exercise — just the one-layer path.
    secret = "AKIA" + "Q" * 16  # pragma: allowlist secret
    try:
        out, n, bd = ns["_redact"]("key=" + secret)
    except Exception as e:  # pragma: no cover
        print(f"  FAIL _redact raised: {e}")
        failures += 1
    else:
        if n < 1 or secret in out or not bd:
            print(f"  FAIL _redact single-layer regression: count={n} out={out!r} bd={bd!r}")
            failures += 1
        else:
            print("  ok   _redact redacts via the single regex layer (count + breakdown)")

    # 4. MCP tool-call/response correlation (_mcp_tool_calls / _mcp_response_entries /
    #    _mcp_tool_responses), exercised against fake flow stand-ins (mcp_flow above) --
    #    no live vmcp/mitmproxy needed, same "exec the real compiled objects" approach
    #    as the rest of this file.
    mcp_tool_calls = ns["_mcp_tool_calls"]
    mcp_tool_responses = ns["_mcp_tool_responses"]
    truncate_for_log = ns["_truncate_for_log"]
    max_len = ns["MAX_MCP_RESPONSE_LOG_LENGTH"]

    call_req = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "kubectl_get", "arguments": {"resource": "pods"}},
    }

    # 4a. Single call, matching JSON result.
    resp = json.dumps({"jsonrpc": "2.0", "id": 1,
                        "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}})
    flow = mcp_flow(call_req, resp)
    calls = mcp_tool_calls(flow)
    if calls != [(1, "kubectl_get", {"resource": "pods"})]:
        print(f"  FAIL mcp calls: unexpected extraction -> {calls!r}")
        failures += 1
    else:
        print("  ok   _mcp_tool_calls extracts (id, tool, args)")
    results = mcp_tool_responses(flow)
    if results != [(1, "kubectl_get", True,
                    {"content": [{"type": "text", "text": "ok"}], "isError": False})]:
        print(f"  FAIL mcp response: matching result -> {results!r}")
        failures += 1
    else:
        print("  ok   mcp response matched by id, ok=True on isError:false result")

    # 4b. JSON-RPC-level error.
    resp = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})
    flow = mcp_flow(call_req, resp)
    results = mcp_tool_responses(flow)
    if results != [(1, "kubectl_get", False, {"code": -32000, "message": "boom"})]:
        print(f"  FAIL mcp response: JSON-RPC error -> {results!r}")
        failures += 1
    else:
        print("  ok   mcp response: JSON-RPC-level error -> ok=False")

    # 4c. MCP tool-level isError:true is a JSON-RPC *result*, not an error key --
    #     must still be surfaced as ok=False.
    resp = json.dumps({"jsonrpc": "2.0", "id": 1,
                        "result": {"content": [{"type": "text", "text": "denied"}], "isError": True}})
    flow = mcp_flow(call_req, resp)
    results = mcp_tool_responses(flow)
    if not results or results[0][2] is not False:
        print(f"  FAIL mcp response: isError:true result should give ok=False -> {results!r}")
        failures += 1
    else:
        print("  ok   mcp response: tool-level isError:true -> ok=False")

    # 4d. Batch matched by id, deliberately out of request/response order -- proves
    #     id-matching, not response-array-position matching.
    batch_req = [
        {"jsonrpc": "2.0", "id": "a", "method": "tools/call",
         "params": {"name": "tool_a", "arguments": {}}},
        {"jsonrpc": "2.0", "id": "b", "method": "tools/call",
         "params": {"name": "tool_b", "arguments": {}}},
    ]
    batch_resp = json.dumps([
        {"jsonrpc": "2.0", "id": "b", "result": {"content": [], "isError": False}},
        {"jsonrpc": "2.0", "id": "a", "result": {"content": [], "isError": False}},
    ])
    flow = mcp_flow(batch_req, batch_resp)
    results = mcp_tool_responses(flow)
    by_tool = {tool: call_id for call_id, tool, _ok, _payload in results}
    if by_tool != {"tool_a": "a", "tool_b": "b"}:
        print(f"  FAIL mcp batch: id-matching failed -> {results!r}")
        failures += 1
    else:
        print("  ok   mcp batch matched by id, not response position")

    # 4e. Single-event text/event-stream framing is unwrapped the same as plain JSON --
    #     the streamable-HTTP transport lets agentgateway answer either way.
    sse_body = "event: message\ndata: " + json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}}
    ) + "\n\n"
    flow = mcp_flow(call_req, sse_body, response_content_type="text/event-stream")
    results = mcp_tool_responses(flow)
    if not results or results[0][2] is not True:
        print(f"  FAIL mcp SSE: single-event data: line not unwrapped -> {results!r}")
        failures += 1
    else:
        print("  ok   mcp SSE-framed (text/event-stream) response unwrapped same as application/json")

    # 4f. _truncate_for_log caps long text with a marker and leaves short text alone.
    short = "short result"
    if truncate_for_log(short, max_len) != short:
        print("  FAIL _truncate_for_log: modified text under the cap")
        failures += 1
    else:
        print("  ok   _truncate_for_log leaves text under the cap untouched")
    long_text = "x" * (max_len + 500)
    truncated = truncate_for_log(long_text, max_len)
    if len(truncated) <= max_len or not truncated.startswith("x" * max_len):
        print(f"  FAIL _truncate_for_log: unexpected output (len={len(truncated)})")
        failures += 1
    else:
        print(f"  ok   _truncate_for_log truncates oversized text ({len(long_text)} bytes) with a marker")

    # 4g. Non-hashable JSON-RPC id (a list -- invalid per spec, but json.loads won't
    #     reject it) must not raise. Proves id-matching is a linear scan, not a dict
    #     keyed by id (which would TypeError: unhashable type).
    weird_req = {"jsonrpc": "2.0", "id": [1, 2], "method": "tools/call",
                 "params": {"name": "kubectl_get", "arguments": {}}}
    weird_resp = json.dumps({"jsonrpc": "2.0", "id": [1, 2],
                              "result": {"content": [], "isError": False}})
    flow = mcp_flow(weird_req, weird_resp)
    try:
        results = mcp_tool_responses(flow)
    except Exception as e:
        print(f"  FAIL mcp non-hashable id raised instead of matching: {e}")
        failures += 1
    else:
        if not results or results[0][0] != [1, 2]:
            print(f"  FAIL mcp non-hashable id: expected a match -> {results!r}")
            failures += 1
        else:
            print("  ok   mcp non-hashable (list) id matches without raising")

    if failures:
        print(f"\nFAIL: {failures} scrub-pattern assertion(s) failed", file=sys.stderr)
        sys.exit(1)
    print("\nPASS: scrub.py regex registry + MCP call/response correlation verified")


if __name__ == "__main__":
    main()
