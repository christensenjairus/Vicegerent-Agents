# Egress Proxy - Security Model

The chart runs two mitmproxy roles from one scrubber ConfigMap. `egress-proxy` sits between every agent sandbox and its outbound HTTP(S) traffic. When webhooks are enabled, `webhook-egress-proxy` sits only between the shared listener and agent webhook Services. Both provide secret scrubbing and audit logs; the ordinary proxy also enforces the external-destination controls below. Neither is a complete security boundary on its own, so both work alongside Cilium network policy and the Sandbox CRD's isolation.

> **Scope**: the ordinary proxy guards every pod in the `agent-sandbox` namespace, whichever harness the sandbox runs. The dedicated webhook proxy accepts only the labeled listener in `webhooks` and can reach only webhook-enabled agent pods on port 8644, plus the Agentgateway judge when prompt-injection detection is enabled. Agents cannot enter the dedicated proxy, and the ordinary proxy cannot reach webhook ports.

---

## What the proxy enforces

### Secrets scrubbing
Applied to every request - headers and body - before forwarding to any destination, internal (agentgateway, searxng) or external (internet). A single regex registry (`REDACT_PATTERNS`) runs on each scrubbed string, compiled at render time from the canonical `images/mcp-cerbos-shim/internal/server/secret-patterns.json` injected via `helm --set-file secretPatterns=…`. That same JSON is embedded into `mcp-cerbos-shim` via `//go:embed`, so the proxy and the shim derive from one source of truth - no hand-sync.

### Webhook prompt-injection detection

The dedicated webhook proxy reads `policy.contentSafety.promptInjection.status` and `judgeModel`. When enabled, it screens the already-redacted body with the canonical `images/mcp-cerbos-shim/internal/promptinjection/patterns.json` registry, calls the Agentgateway OpenAI judge only for regex matches, rejects confirmed injections, fails open on judge-service errors, and rejects a payload if more than 20 candidates would require verification. The ordinary agent proxy never runs this webhook gate.

Coverage spans SSH private keys; Slack tokens; HTTP `Bearer`/`Basic` auth values; cloud and SaaS provider credentials (AWS, GitHub - classic and fine-grained - GitLab, Google, OpenAI, Anthropic, Stripe, Notion, Twilio, npm, Okta, Atlassian, Databricks, Azure Storage/Entra, Elastic, database/broker connection URIs with an inline password, JFrog, Grafana, Docker Hub, PyPI, Hugging Face, 1Password, Linear, PagerDuty); generic JWTs; and PII (US SSN, Visa/Mastercard/Amex/Discover card numbers, US phone numbers). The canonical `images/mcp-cerbos-shim/internal/server/secret-patterns.json` is the authoritative per-pattern list - read it directly for exact names and regexes rather than relying on this summary.

Header stripping is separate from the registry and unconditional - no regex has to match. The `Authorization` header is scrubbed on every request regardless of destination (agentgateway injects the real upstream provider key on its own outbound leg, so an auth header on an agent request is only ever a secret), and so are the `x-api-key` and `api-key` headers.

The request **URL path and query** are also scrubbed on every request, and response bodies are scrubbed (non-streaming only) to guard against echo attacks - both through the same `_redact()`.

### Method enforcement
GET and HEAD only for external destinations. POST, PUT, PATCH, DELETE → 403. Internal cluster services (agentgateway, searxng) may use any method - they require POST and hold no sandbox secrets. Exception: `git-upload-pack` (smart-HTTP clone/fetch, read-only) is allowed through so `pre-commit` can install hook repos.

### URL length limit
External URLs over 2048 characters → 403. Blocks naive base64/hex query-string exfiltration. All legitimate API and search URLs are well under this limit.

### GET/HEAD body blocking
GET and HEAD requests with a non-empty body to external destinations → 403. GET bodies have no legitimate use case here and are a potential exfiltration channel.

### WebSocket blocking
`Upgrade: websocket` headers → 403 in the `request()` hook. `websocket_start` hook kills any connection that slips through. Applies everywhere.

### SSRF protection
Requests to RFC1918, link-local (169.254/16), loopback, CGNAT (100.64/10), and their IPv6 counterparts (`::1/128`, `fc00::/7`, `fe80::/10`) → 403. Defence-in-depth alongside Cilium's `egressDeny` rules.

### Audit log
Every request/response emits a JSON log line (one object per line, `message` carries the same content the pre-JSON format used):
```
{"time": "2026-07-08T22:14:03+0000", "level": "INFO", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 ALLOW internal=False method=GET url=https://pypi.org/simple/requests/"}
{"time": "2026-07-08T22:14:03+0000", "level": "WARNING", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 BLOCKED method=POST url=https://api.github.com/repos/..."}
{"time": "2026-07-08T22:14:03+0000", "level": "INFO", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 RESPONSE method=GET status=200 url=https://pypi.org/simple/requests/"}
{"time": "2026-07-08T22:14:03+0000", "level": "WARNING", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 RESPONSE-REDACTED count=1 patterns=jwt:1 method=GET status=200 url=https://pypi.org/simple/requests/"}
```
View with: `kubectl logs -n egress-proxy deploy/egress-proxy` - pipe through `jq` for readability, e.g. `... | jq -r .message`.

MCP tool calls (`tools/call` through vmcp) get a dedicated pair of lines instead of the generic `ALLOW`/`RESPONSE`, correlated by the JSON-RPC `id` (echoed in both - `client=ip:port` alone doesn't disambiguate concurrent or keep-alive calls from the same sandbox):
```
{"time": "...", "level": "INFO", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 MCP-CALL tool=kubectl_get id=7 args={\"resource\":\"pods\",\"namespace\":\"agent-sandbox\"} url=http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp"}
{"time": "...", "level": "INFO", "logger": "egress-proxy", "message": "client=10.1.2.3:41822 MCP-RESPONSE tool=kubectl_get id=7 ok=True result={\"content\":[{\"type\":\"text\",\"text\":\"NAME ... \"}],\"isError\":false} status=200 url=http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp"}
```
`ok=False` for either a JSON-RPC-level `error` or an MCP tool-level `isError:true` result - grep `MCP-RESPONSE.*ok=False` to find failed calls without wading through the full result bodies. `result=` is truncated at 32KB (`...(truncated, N bytes total)` appended; the truncated form is for grep/human reading only, not re-parseable JSON) - sized to bound a pathological single response, not aggregate log volume, so ordinary tool output is never truncated. `result=` reflects content already redacted **twice** upstream of this line: once by `mcp-cerbos-shim`'s own independent `CheckResponse` redaction (before the response reaches agentgateway), and once by this proxy's own response-body redaction (see Secrets scrubbing above) - "exactly what the agent received," not the tool's raw first-hand output. MCP-RESPONSE parses both a plain `application/json` body and a single-event `text/event-stream` body (unlike the response-body redaction above, which skips SSE entirely to avoid buffering issues) - the streamable-HTTP transport lets agentgateway answer a `tools/call` either way.

---

## What the proxy does NOT enforce

### Destination content policy
The proxy checks the HTTP *method*, not the *response content*, and it path-scopes only the hosts that need it. A GET to an allowed FQDN succeeds regardless of path unless that host has an `EXTERNAL_PATH_SCOPES` entry in the addon - a per-host prefix allowlist for multi-tenant destinations, where Cilium's host-only `toFQDNs` entry would otherwise grant access to every path any tenant could serve (currently just `storage.googleapis.com`, scoped to `/proxy-golang-org-prod/` for the Go module proxy's zip redirect). Hosts with no entry stay unrestricted, deliberately - blanket path policy needs constant maintenance and breaks legitimate use.

**Mitigation**: The Cilium FQDN allowlist (rendered into `charts/egress-proxy/templates/networkpolicy.yaml`) is the destination gate. Only explicitly listed FQDNs are reachable. Add FQDNs in `values.yaml`'s `egress:` block, not URL path rules - see [Adding a new external service](#adding-a-new-external-service). Add an `EXTERNAL_PATH_SCOPES` entry only when the new host is multi-tenant.

### Sophisticated GET exfiltration
A URL within the 2048-char limit can still carry meaningful data in query strings or path segments. Encoding (base64, hex, split-chunking) bypasses pattern scrubbing. This is a fundamental limitation of HTTP-layer inspection.

**Accepted risk**: The FQDN allowlist limits the set of reachable destinations. Exfiltration requires a reachable destination that accepts and stores GET parameters - an attacker needs prior access to configure such an endpoint.

### Secrets not caught by the registry
The regex registry covers the named provider shapes in the table above. A credential in none of them - a bespoke internal token format, or any secret carried **encoded** (base64, hex, split-chunked) - still passes through. Pattern matching is raw-value only.

**To add a regex pattern**: edit the canonical `images/mcp-cerbos-shim/internal/server/secret-patterns.json` - one edit covers both this proxy (injected via `--set-file`) and the shim (`//go:embed`). For verbatim secret values, see below.

**To scrub a literal secret value**: there is currently no mechanism to inject runtime secret values into the proxy for scrubbing. Adding this requires mounting the secret into the proxy pod and loading it at startup - a future improvement.

### SSH traffic
Port 22 egress is direct - it bypasses the proxy entirely. Each key under `directEgress.ssh.hosts` is an allowed SSH connection hostname, and that host's `cnameChain` supplies only its intermediate DNS aliases. Defaults allow no SSH host; each machine values file explicitly lists `github.com` and any additional forge it needs. CNAME entries are not additional git-host slots. `git push` content is not inspectable at the HTTP layer. The SSH deploy key's scope (read-only vs read-write, and repository allowlist) remains the primary control for pushes.

### Slack traffic
Slack FQDNs are allowed direct (bypassing the proxy) through the per-agent Cilium policy (`charts/agent/templates/networkpolicy.yaml`, FQDNs set through `directEgress.slackFQDNs`) and `no_proxy` in `charts/agent/templates/_sandbox.tpl`. The list defaults to empty in `values.defaults.yaml`; `values.example.yaml` retains the four endpoints below so Slack works if its optional credentials are configured. Those destinations alone do not enable Slack or grant user access. Slack Socket Mode requires POST and WebSocket - both blocked by the proxy - so Slack must go direct. (`no_proxy` alone is not enough: `slack_sdk` ignores `NO_PROXY` and auto-loads `HTTPS_PROXY`, so the agent image also carries build patch `0007-slack-bypass-egress-proxy.py` to force the bypass.)

| FQDN | Purpose |
|---|---|
| `slack.com` | Web API (`slack.com/api/*`) - all bot API calls |
| `wss-primary.slack.com` | Socket Mode WebSocket (primary endpoint) |
| `wss-backup.slack.com` | Socket Mode WebSocket (failover endpoint) |
| `files.slack.com` | File/image downloads for attachment handling |

The former `*.slack.com` wildcard is removed from the Cilium policy. If Slack rotates the WSS hostname, Socket Mode reconnections will fail - add the new hostname to the agent's `directEgress.slackFQDNs` in `values.yaml`. `no_proxy` needs no edit for a `*.slack.com` name: it carries the suffix form `.slack.com`, which already covers any new subdomain. Slack traffic carries no sandbox secrets by design.

### Streaming responses
SSE (`text/event-stream`) and chunked transfer responses skip response body scrubbing to avoid buffering the LLM stream. An echo attack via streaming is theoretically possible but requires the external server to actively reflect back injected content.

---

## Bypass vectors

### Cannot bypass (Cilium enforces at kernel level)
- Direct TCP to internet FQDNs not in the allowlist - Cilium drops the packet
- Direct TCP to internet IPs (bypassing proxy) - Cilium allows only proxy:8080 from sandbox
- Non-HTTP protocols on port 443 - Cilium allows the port but mitmproxy rejects non-HTTP

### Difficult to exploit in practice
- **GET query string exfiltration** - URL length limit constrains payload size; destination must be in FQDN allowlist and must store/forward the data
- **Encoded secrets** - scrubbing patterns match raw values; base64/hex encoding evades them, but encoding is a deliberate extra step requiring tool access

### Residual risks
- **DNS rebinding** - `_is_private()` in the addon only checks literal IP addresses; hostname-based requests bypass it (intentional - Cilium enforces the destination boundary). Cilium FQDN policy caches DNS results with a TTL; within that window a rebinding attack (attacker-controlled FQDN, TTL 0, rebind to RFC1918) could reach a private IP via an allowed FQDN. The `egressDeny` CiliumNetworkPolicy rules are the actual guard here - they operate at the packet level and block the private-IP egress regardless of what DNS returned. Short exploitation window; requires attacker-controlled DNS infrastructure.
- **`no_proxy` override in subprocesses** - a subprocess could set `NO_PROXY=*`, causing it to attempt direct egress which Cilium then drops. Fails noisily rather than silently exfiltrating.
- **IPv6** - the sandbox pod has no IPv6-specific FQDN allowlist. However, the `egressDeny` CiliumNetworkPolicy includes `::1/128`, `fc00::/7`, and `fe80::/10` to block private IPv6 ranges. Direct IPv6 internet egress from tools that ignore `http_proxy` would need a non-private IPv6 destination; the Cilium default deny covers the rest.

---

## Adding a new external service

There are two CiliumNetworkPolicies; pick by how the sandbox reaches the service.

For a service the **proxy fetches** (GET/HEAD through the egress proxy):
1. Add the FQDN to `values.yaml`'s `egress:` block - `wildcardDomains` if the service also needs subdomains, or `exactDomains` for an exact host only. Both are YAML lists and feed the Cilium policy and `scrub.py` from the same source.
2. If the service needs POST it cannot go through the proxy (external POST → 403) - route it direct instead (below)
3. If the service holds credentials, add its token format to `images/mcp-cerbos-shim/internal/server/secret-patterns.json` - see [Adding a new secret pattern](#adding-a-new-secret-pattern).
4. If the service is multi-tenant (object storage, a CDN), add an `EXTERNAL_PATH_SCOPES` entry so the host-only FQDN allowlist doesn't grant every tenant's path.

For a service the sandbox reaches **direct** (bypassing the proxy - needed when the service requires POST or WebSocket the GET-only proxy can't relay, e.g. Slack or edge-tts):
1. Add a new `directEgress.<service>FQDNs` field in `values.yaml`, defaulting to an empty list (opt-in), and a matching `toFQDNs` block in `charts/agent/templates/networkpolicy.yaml` - see `directEgress.slackFQDNs` and `directEgress.edgeTtsFQDNs` for the pattern; each direct-bypass channel gets its own field and its own Cilium block.
2. If the client library honors the proxy env vars, add the FQDN(s) to `no_proxy`/`NO_PROXY` in `charts/agent/templates/_sandbox.tpl` (see the Slack entry there). If it doesn't respect `NO_PROXY` at all (like `slack_sdk`, which also auto-loads `HTTPS_PROXY`), add a build patch under `images/agent/patches/` to force the bypass instead - see `0007-slack-bypass-egress-proxy.py`.

---

## Adding a new secret pattern

Append an object to the canonical `images/mcp-cerbos-shim/internal/server/secret-patterns.json`, e.g.:

```json
{"name": "mycorp_token", "regex": "mycorp_tok_[A-Za-z0-9]{32}"}
```

One edit covers both legs: the shim embeds the file via `//go:embed` and the installer/validator inject it into this proxy via `helm --set-file secretPatterns=…`. Keep the regex RE2-safe (no lookaround or backreferences) so it compiles under both Go's `regexp` and Python's `re` - put `(?i)` inline where you need case-insensitivity. Add a matching fixture to `scripts/test-scrub-patterns.py`. Reloader restarts the proxy pod automatically when the rendered ConfigMap changes.
