# Webhook listener

`webhook-listener` is Vicegerent's shared, in-cluster ingress for provider webhooks. It opens one outbound ngrok tunnel, verifies each request, and routes `/webhooks/<agent>/<route>` through the dedicated webhook scrubber to that agent's fixed ClusterIP Service on port 8644.

## Trust boundary

The listener is the only workload that receives provider signing material. Its Deployment mounts the shared `webhooks/vicegerent-webhook-secrets` Secret read-only and reads the route-derived `<agent>__<route>` key for every request, so Secret rotation is observed without placing the value in an environment variable or restarting an agent. The shared ngrok authtoken comes from `webhooks/vicegerent-ngrok-authtoken:authtoken`.

After a signature succeeds, the listener strips every recognized signature header and forwards the original method, ordinary headers, query string, and raw body using an explicit `WEBHOOK_FORWARD_PROXY` transport. Targets are validated at startup against the exact `http://<agent>-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/<route>` shape, preventing a ConfigMap change from turning the listener into an arbitrary-network relay. The dedicated proxy redacts recognized secrets before forwarding and optionally screens the redacted body for prompt injection.

The listener runs as uid/gid 65532 with a read-only root filesystem, no capabilities, and no service-account token. Cilium allows it to resolve DNS, connect to `connect.ngrok-agent.com:443`, and connect to `webhook-egress-proxy:8080`; it has no direct agent egress. Only the listener may enter the dedicated proxy. Each agent's reciprocal policy accepts port 8644 only from that proxy and only for the agent's configured HTTP route paths. Agent sandboxes cannot reach the listener or dedicated proxy, while their ordinary egress proxy cannot reach agent webhook ports. The listener has no in-cluster Service or ingress rule; `/healthz` is also served on the public listener for tunnel diagnostics, while Kubernetes probes use a separate loopback-only endpoint. Its Deployment uses the `Recreate` strategy so a configuration or token rollout cannot briefly race two pods for the one shared ngrok endpoint.

Each configured-route delivery logs an outcome, agent, route, provider, status, request byte count, and duration. Vector collects these lines into VictoriaLogs. Request bodies, provider signature headers, and signing secrets are never logged.

## Routing configuration

The chart renders `/etc/vicegerent-webhooks/routes.json` from the global `webhooks.publicUrl` and each enabled `agents[].webhooks.routes` entry. The external provider, route-derived Secret file, and fixed target remain in this listener ConfigMap. The agent chart removes `provider`, injects only `trusted_proxy: true` plus delivery/prompt metadata into Hermes, and rejects attempts to set internal signing fields directly.

Supported providers:

- `pagerduty`: HMAC-SHA256 of the raw body, accepting any valid `v1=` candidate in `X-PagerDuty-Signature` during rotation.
- `github`: HMAC-SHA256 of the raw body in `X-Hub-Signature-256` as `sha256=<hex>`.
- `gitlab`: constant-time comparison of `X-Gitlab-Token`; GitLab's native protocol authenticates the token but does not bind the body cryptographically.
- `svix`: HMAC-SHA256 of `<id>.<timestamp>.<body>`, supporting raw and `whsec_` base64 secrets plus multiple rotation signatures.
- `generic-v2`: HMAC-SHA256 of `<timestamp>.<body>` in `X-Webhook-Signature-V2`.
- `alertmanager`: constant-time comparison of the credential in the `Authorization` header. Alertmanager signs no payload, so this authenticates the sender without binding the body; the header is stripped before forwarding so the credential never reaches the agent.

Svix and generic V2 reject timestamps outside a five-minute replay window. Payloads are capped at 1 MiB. Unknown agents, unknown routes, disabled routes, and unsupported methods return the same 404 shape so the endpoint does not expose route membership.

## Build and test

The image name and immutable tag live in `Makefile`. `make check` is CI parity and runs formatting, `go vet`, and the full unit/proxy suite.

```bash
make check
make image
make release
```

The tests cover fixed known-good signatures, wrong secrets, body tampering, timestamp replay, rotation formats, signature-header stripping, body/query preservation, an explicitly pinned forward proxy, indistinguishable unknown routes, failure-before-forwarding, and cross-agent target rejection. Repository-level Helm, scrubbing, prompt-injection, and Cilium assertions live in `scripts/validate-webhook-ingress.py` and `scripts/test-scrub-patterns.py`.
