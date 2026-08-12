# Architecture and security model

Vicegerent is designed for AI agents that need meaningful access to real systems without relying on the agent to police itself. This document describes the security objective, trust boundaries, enforcement layers, request paths, and accepted limitations. See the [README](../README.md) for the product overview and [setup guide](setup.md) for installation and operation.

## Security objective

Vicegerent assumes the agent can be wrong, prompt-injected, or fully compromised within its runtime identity. A compromised agent may inspect everything readable in its pod, modify its writable volumes, execute arbitrary unprivileged processes, invoke every MCP tool exposed to it, and attempt any network connection its process can originate.

The platform's objective is to constrain the effects of those actions through controls the agent cannot modify:

- Limit filesystem access and container privileges inside the sandbox.
- Limit the network destinations and protocols reachable from the sandbox.
- Expose only the MCP capabilities selected for the agent.
- Authorize each mapped MCP call against its resource and arguments.
- Keep model-provider and MCP integration credentials outside the agent process where the architecture permits it.

The host, Kind cluster, Kubernetes and Cilium control planes, installed controllers, policy configuration, agentgateway, Cerbos, the host-side MCP control plane, and their administrators remain trusted. Vicegerent is not a defense against a compromised host, cluster administrator, controller, policy author, or upstream service.

The platform also does not prove that an agent's decisions or code are correct. An agent can cause damage within an explicitly granted capability. The goal is bounded authority, not infallibility.

## Request paths

The long-running agent process sits inside an `agent-sandbox` pod. Its main external paths are:

```text
Model request
    Agent → egress proxy → agentgateway → model provider

MCP request
    Agent → egress proxy → agentgateway
          → mcp-cerbos-shim → Cerbos
          → host vMCP over mTLS → selected MCP service → external system

Explicit direct egress
    Agent → Cilium-approved SSH, Slack, or text-to-speech destination
```

The shim is an agentgateway guardrail, not a separate route the agent chooses. For the public `vmcp` backend, the guardrail runs in `FailClosed` mode; a guardrail failure prevents the call from being forwarded.

## Incoming webhooks

Incoming provider webhooks use one stable Cloudflare Tunnel HTTPS origin for the cluster. Separate `cloudflared` and `webhook-listener` Deployments run in the `webhooks` namespace. `cloudflared` opens the outbound tunnel and forwards it to the listener's ClusterIP Service. The listener maps `/webhooks/<agent>/<route>` to a fixed agent Service, authenticates the provider-native signature, strips signature headers, and sends the request through a dedicated `webhook-egress-proxy` before Hermes receives it on port 8644. That proxy uses the platform's canonical secret registry to redact headers, query values, and bodies. PagerDuty, GitHub, GitLab, Svix, Alertmanager, and the timestamped generic V2 format are supported; GitLab's token authenticates the delivery but, by that provider's protocol, does not cryptographically bind the body.

Signing material terminates at the listener and never enters an agent Secret, ConfigMap, environment, or volume. Setup derives one key per active route in the shared `webhooks/vicegerent-webhook-secrets` Secret and the listener rereads the mounted file for every request, so Kubernetes Secret rotation does not require an agent restart. Hermes receives a secretless route marked `trusted_proxy`; a narrow image patch rejects any route that combines that marker with signing material.

The network boundary is load-bearing. Neither workload has a Kubernetes API token. `cloudflared` can reach only DNS, the Cloudflare edge (`region1.v2.argotunnel.com` and `region2.v2.argotunnel.com` on port 7844), and the listener Service on port 8081; it cannot reach the dedicated webhook proxy. The listener accepts that port only from `cloudflared` and can reach only DNS and the dedicated proxy. The tunnel is locally managed: its only ingress mapping lives in a Git-reviewed ConfigMap that forwards the configured hostname to the listener Service and answers everything else with 404, so a compromised Cloudflare account cannot repoint public traffic at another in-cluster target, and the mounted credentials run only this one tunnel. Tunnel credentials mount only into `cloudflared`, while signing material mounts only into the listener. Only the listener can enter the dedicated proxy; only that proxy can reach webhook-enabled agent pods on port 8644. Each destination agent adds an HTTP L7 allowlist for its own configured `POST /webhooks/<route>` paths. Agent sandboxes have no egress to the listener or dedicated proxy, and their shared egress proxy has no egress to agent webhook ports, so an agent cannot use either proxy to enter another agent's route. When prompt-injection detection is enabled, only the dedicated proxy gains port 80 egress to the Agentgateway judge. There is no public LoadBalancer, host port-forward, or laptop-supervised tunnel process, so webhook availability follows the Kind cluster rather than the host MCP stack.

The optional webhook prompt-injection gate uses the existing `policy.contentSafety.promptInjection` status and judge model. Secret redaction runs first, then a broad regex prefilter compiled from the same canonical JSON embedded by `mcp-cerbos-shim`; only matches reach the LLM judge. A confirmed injection is rejected, judge-service errors fail open, and exhausting the bounded verification budget fails closed.

## Four independent enforcement layers

### 1. Filesystem and container privilege controls

Each configured agent is rendered as its own Agent Sandbox resource and pod. The long-running container runs as uid and gid 10000 with `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, a read-only root filesystem, every Linux capability dropped, and the `RuntimeDefault` seccomp profile. `hostUsers: false` places pod identities in a private user namespace, and `automountServiceAccountToken: false` prevents Kubernetes from mounting a workload API token.

Writable state is limited to explicit mounts such as `/opt/data`, `/workspace`, `/tmp`, and runtime volumes. Project-owned configuration and the SSH key are mounted read-only where practical. A startup-only ownership-repair init container runs as uid 0 inside the private user namespace with only `CHOWN` and `DAC_OVERRIDE`; it does not change the unprivileged identity of the long-running agent.

These are container-level privilege and filesystem controls, not per-process isolation. Vicegerent does not implement a process allowlist or isolate one agent subprocess from another inside the pod. Hermes, Claude Code, Codex, OpenCode, and their subprocesses all operate within the same pod boundary.

### 2. Network egress control

The sandbox does not have general network access. Its Cilium policy permits DNS for configured names, TCP to the egress proxy, and narrowly configured direct channels. Traffic that does not match an allow rule is unavailable at the network layer.

HTTP and HTTPS requests normally pass through the egress proxy. For external destinations, the proxy enforces an FQDN allowlist, method and request-body restrictions, a URL-length limit, WebSocket blocking, literal private-address checks, recognized-secret scrubbing, and audit logging. Cilium adds a private-range deny as a network-layer backstop. Internal platform services use the methods they require but still pass through request scrubbing.

Cilium and the proxy solve different problems. Cilium determines which endpoints and ports are reachable even if a process ignores its proxy settings. The proxy inspects supported HTTP traffic. Neither should be treated as a replacement for the other.

Some configured traffic deliberately bypasses the proxy: git over SSH, Slack's POST and WebSocket traffic, and optional text-to-speech WebSockets. Those paths remain destination- and port-scoped by Cilium but do not receive the proxy's content inspection or scrubbing. The [egress proxy documentation](../charts/egress-proxy/README.md) describes the complete controls, exceptions, and residual exfiltration risks.

### 3. MCP capability selection

External integrations run as host-side ToolHive workloads and are aggregated into a virtual MCP server. Each backend entry in `host/mcp/toolhive-servers.json` declares the tool names intended for the sandbox surface. The generated vMCP configuration exposes that selected set rather than every operation an upstream MCP server implements.

This makes capability selection explicit. A Git provider can expose pull-request creation and issue reads without also exposing merge, repository-administration, or comment operations. Current integrations include source control, Kubernetes, work management, monitoring, incident response, documents, cloud APIs, and web research; the configuration file is the source of truth for the exact enabled backends and tools.

Tool selection answers “does this operation exist for the agent?” It does not answer “is this invocation allowed?” That is the next layer.

### 4. Per-call MCP authorization

Every public MCP tool invocation passes through `mcp-cerbos-shim` before agentgateway forwards it. The mapping identifies the resource and action, normalizes fields when necessary, and passes the resulting attributes to Cerbos. Policies can restrict repositories, projects, teams, assignees, services, page ancestry, data sources, namespaces, and other integration-specific resources.

Mapped tools may also define a `force` block. A forced argument rewrite runs only after Cerbos allows the call and cannot override a denial. The current GitHub mapping, for example, forces `draft: true` on pull-request creation. This is materially different from asking the model in a prompt to create drafts: the upstream service receives the policy-constrained request regardless of what the model requested.

The shim also applies response-side controls including secret and PII redaction, content moderation when enabled, and prompt-injection detection when enabled. Detailed mappings, lookup behavior, helper functions, and known constraints live in the [`mcp-cerbos-shim` documentation](../images/mcp-cerbos-shim/README.md).

## Credential boundaries

Vicegerent deliberately separates a capability from the reusable credential behind it where possible.

Model-provider API keys are stored in Kubernetes Secrets referenced by agentgateway's provider backends. Agent harnesses point at the in-cluster gateway and use non-secret placeholder values where a client insists on a credential-shaped setting. The real provider key is added by the gateway on its upstream leg.

MCP API keys, OAuth sessions, kubeconfigs, and AWS sessions live with the host-side ToolHive workloads. The agent calls the aggregated MCP endpoint through agentgateway and does not need those integration credentials in its filesystem or environment.

The sandbox is not credential-free. Each agent has its own dashboard authentication and SSH key. Optional Slack integration places its required Slack values in the agent Secret because the runtime itself maintains that connection. Workspace files and user-authored configuration may contain other sensitive material, and a compromised agent can read anything its runtime identity can read.

External credential placement reduces what the agent can steal and reuse directly, but it does not reduce the authority of an exposed capability. A credentialless request to an overpowered tool is still overpowered. That is why credential isolation is paired with tool selection, per-call authorization, and network policy.

## Compromise walkthrough

The following controls act independently when a compromised agent attempts common escalation paths:

| Agent action | Expected boundary |
|---|---|
| Search the environment for a model-provider or MCP token | Real provider and MCP integration credentials are held by agentgateway or host-side ToolHive workloads |
| Read the Kubernetes API token | No service-account token is mounted in the agent pod |
| Open a connection to an arbitrary internet or internal destination | The sandbox Cilium policy has no matching egress rule |
| Bypass the HTTP proxy for an otherwise allowed external host | Cilium still restricts the direct destination and port; only explicit direct channels are available |
| Invoke a tool outside the selected vMCP surface | The tool is not advertised or callable through the sandbox vMCP |
| Invoke an exposed tool against a forbidden resource | Cerbos denies the normalized action and resource |
| Create an allowed GitHub pull request with `draft: false` | The mapping rewrites the authorized call to `draft: true` |

No single row is the whole security model. The value comes from a compromised process having to contend with independent runtime, network, capability, and authorization boundaries.

## Supervised and unattended execution

The sandbox vMCP is intended for autonomous or near-autonomous work: scheduled jobs, event-driven runs, and sessions where a person will not inspect every command and tool call. It uses the selected tool surface and routes calls through agentgateway and Cerbos.

The optional operator vMCP serves native laptop harnesses under active human supervision. It aggregates the same host backends and provides the token-saving tool-discovery interface, but deliberately omits the sandbox tool filter and bypasses agentgateway and Cerbos. This repository does not claim that endpoint is policy-scoped. If a person will not supervise every action, the work belongs in the sandbox rather than on the operator endpoint.

## Harness independence

Vicegerent is a sandbox platform, not an agent framework. It does not decide how an agent reasons, plan tasks, or replace the harness. The installed agent image supports Hermes, Claude Code, Codex, and OpenCode inside the same pod boundary.

Hermes adds its own layered command-approval pipeline. That is useful defense in depth when Hermes is the active harness, but it is an in-agent control and is not treated as the platform's containment boundary.

The harnesses share persistent knowledge through the agent data volume. Mnemosyne and the Obsidian vault are single stores, while a publication and adoption mechanism makes shared skills discoverable across harnesses despite their different filesystem-scanning behavior. See [Shared skills and recovery](../images/agent/README.md#shared-skills-and-recovery) for that mechanism and its recovery path.

## Vicegerent and OpenShell

NVIDIA OpenShell is the closest adjacent open-source project. Its overview describes a gateway-managed runtime with Docker, Podman, MicroVM, and Kubernetes compute backends, plus declarative policy domains for filesystem, network, process, and inference controls.[1]

Vicegerent makes a narrower set of architectural commitments: a local Kubernetes and Kind deployment, Cilium plus an HTTP egress proxy, host-side MCP workloads, and agentgateway/Cerbos authorization over the resource and arguments of individual MCP calls. OpenShell's documented policy model is broader across runtime backends; Vicegerent's distinctive emphasis is the externally authorized MCP capability path.

The comparison is architectural, not a claim that either project is a strict superset of the other. Both projects are evolving, and OpenShell's own documentation is the source of truth for its current capabilities.

## Operational design choices

### Why MCP services run on the host

Many useful integrations depend on browser OAuth, a local kubeconfig, AWS SSO, or another session already available on the operator's machine. Running those MCP services under ToolHive lets Vicegerent use that identity without copying its credentials into the sandbox. A ghostunnel mTLS bridge exposes the single aggregated vMCP endpoint to agentgateway.

The tradeoff is availability: host-backed MCP integrations work only while the host control plane is running. An organization with service identities for an integration can move that service into infrastructure it operates continuously, but that is not the repository's current default architecture.

### Declarative configuration and writable harness state

Committed `values.defaults.yaml` defines the annotated platform defaults, while each machine supplies a gitignored `values.yaml` delta. The staged installer renders that configuration into the cluster and can be rerun after changes.

Harness configuration remains writable on the data volume because the harnesses persist preferences and runtime state. Pod initialization reconciles project-owned subtrees exactly while preserving harness-owned fields. The ownership rules live in `charts/agent/files/reconcile-config.py` and are exercised by `scripts/validate-config-reconciliation.py`.

### Parallel MCP calls

Hermes, Codex, and OpenCode can issue independent vMCP calls concurrently. Claude Code parallelizes tools it understands as read-only, while the generic vMCP call surface may perform writes; the managed stdio bridge therefore exposes a bounded batch operation for up to eight independent calls without mislabeling a potentially mutating tool as read-only. Dependent calls remain sequential.

## Limitations and accepted tradeoffs

- Vicegerent has more moving parts than a laptop CLI or a plain container: Kind, Kubernetes controllers, Cilium, Helm, agentgateway, ToolHive, Cerbos, the shim, and the egress proxy all have operational cost.
- A policy-authoring mistake can grant more authority than intended. Configuration review and policy tests are part of the security process.
- Allowed capabilities remain dangerous within their allowed scope. Draft enforcement does not make a bad code change correct, and read access can still expose sensitive data.
- Processes inside an agent pod are not isolated from one another and share the pod's runtime identity and readable mounts.
- The sandbox contains its per-agent SSH and dashboard credentials and may contain Slack credentials. Direct SSH, Slack, and text-to-speech routes bypass HTTP content inspection.
- Pattern-based redaction cannot recognize every secret or encoded payload. Streaming responses are not body-scrubbed by the egress proxy, and an allowed GET destination can still be an exfiltration channel.
- Host-side MCP services create a dependency on the operator's machine and its authenticated sessions.
- The project currently targets a local Kind cluster and is not a hosted multi-tenant service.

## Network policy authoring

Cilium treats DNS permission and connection permission as separate controls. A workload needs both a `toEndpoints` or `toFQDNs` connection rule and a matching DNS rule for every hostname it resolves. Adding only one side either leaves DNS blocked or grants no usable connection.

Prefer exact `matchName` entries for in-cluster services. Cilium's `matchPattern` wildcard replaces one DNS label and does not cross dots, so `*.svc.cluster.local` does not match a two-label service name such as `egress-proxy.egress-proxy.svc.cluster.local`.

The agent Sandbox also sets `dnsConfig.options.ndots:1`. With Kubernetes' default `ndots:5`, a resolver tries search-domain-expanded variants before a four-label cluster FQDN; clients with short timeouts or musl-based resolution can fail before reaching the exact allowed name. Preserve `ndots:1` while the policy relies on exact in-cluster FQDNs.

## Sources

[1] https://github.com/NVIDIA/OpenShell - NVIDIA OpenShell repository
