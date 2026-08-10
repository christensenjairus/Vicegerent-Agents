# Vicegerent Agents

**A security sandbox for AI agents with real-world access.**

Give a coding agent your repositories, cloud APIs, ticket tracker, and monitoring stack, and it can do real work. Let it do that work unattended — on a schedule, triggered by events, with nobody approving each individual action — and you have a harder problem. The agent's permissions are now enforced by the agent's own judgment.

Vicegerent moves that boundary out of the agent. Each agent runs in its own Kubernetes sandbox where filesystem access, network egress, available MCP tools, and the arguments those tools are invoked with are all enforced by infrastructure outside the agent's control.

> The goal isn't to make the agent trustworthy. It's to make the agent's authority enforceable when the agent is wrong, manipulated, or compromised.

## The core idea

A typical agent carries its own capabilities. Its filesystem, credentials, network access, and tools all live inside the process you are asking to behave well:

```text
┌─────────────────────────────┐
│          AI Agent           │
│                             │
│  filesystem                 │
│  credentials                │
│  network                    │
│  tools                      │
└──────────────┬──────────────┘
               │
               ▼
         Your systems
```

The more autonomous that agent becomes, the more uncomfortable the arrangement gets. Vicegerent puts the enforcement boundary around it instead:

```text
┌─────────────────────────────┐
│          AI Agent           │
│                             │
│   "I want to do X."         │
└──────────────┬──────────────┘
               │
     ┌─────────▼─────────┐
     │    Vicegerent     │
     │      Sandbox      │
     │                   │
     │    Filesystem     │
     │      Network      │
     │     MCP tools     │
     │   Authorization   │
     └─────────┬─────────┘
               │
               ▼
        Your systems
```

The agent decides what it wants to do. The infrastructure decides what it is actually allowed to do.

## Four independent enforcement layers

| Layer | Controls | Enforced by |
| --- | --- | --- |
| **Filesystem** | What the agent can touch inside its sandbox | Kubernetes + container runtime |
| **Network** | Which destinations the sandbox can reach | Cilium + egress proxy |
| **MCP tools** | Which tools are exposed to the agent at all | ToolHive vMCP |
| **Tool authorization** | What an exposed tool is allowed to do | Cerbos + `mcp-cerbos-shim` |

These are four separate enforcement points, not four settings in one config file. A model can request an action without being able to reach the policy that decides whether the action succeeds.

### 1. Filesystem containment

Each agent gets its own pod in the `agent-sandbox` namespace, running as a non-root user with `allowPrivilegeEscalation: false`, a read-only root filesystem, every Linux capability dropped, a `RuntimeDefault` seccomp profile, and no automatically mounted service-account token. Writable space is explicit and small: `/opt/data`, `/workspace`, and `/tmp`. Everything else in the container is immutable.

Because the kubelet and the kernel enforce this against the whole container, it holds for whatever the pod happens to be running. Vicegerent does not isolate processes from each other *inside* a pod — the boundary is the pod, not the process.

### 2. Network egress control

The sandbox does not get general internet access. Two mechanisms stack here, and the distinction matters: Cilium decides which destinations the sandbox can reach at all, at the packet level, while an HTTP egress proxy decides what a request to an allowed destination may look like — method restrictions, secret scrubbing, SSRF protection, no WebSocket upgrades.

Because Cilium sits underneath, getting around the HTTP proxy does not turn the sandbox into an unrestricted network client. It just means reaching the same short list of destinations without the proxy's inspection.

Some traffic genuinely can't use a GET/HEAD HTTP proxy. Git over SSH — and Slack and edge text-to-speech when you enable them — connects directly. Cilium still pins those paths to specific destinations and ports, but they do not get content inspection or secret scrubbing, and they are opt-in for that reason.

### 3. MCP capability selection

MCP is how agents reach external systems here, and ToolHive's vMCP hands the sandbox a specific list of tools rather than a whole integration. An agent working on pull requests might receive exactly:

```text
github_create_pull_request
github_pull_request_read
github_issue_read
```

That is a working subset of the GitHub MCP server, not all of it. The same approach applies to GitLab, Kubernetes, Jira, Linear, Grafana, Notion, PagerDuty, AWS, and the rest of the configured backends. The agent gets a capability without inheriting every capability behind it.

### 4. Per-call authorization

Allowlisting tools isn't sufficient, because a legitimate tool can be called illegitimately. Every MCP call therefore passes through `mcp-cerbos-shim` and Cerbos, which can deny it, restrict it to particular resources, inspect its arguments, or rewrite those arguments before the call is forwarded.

The reference example: an agent is allowed to open a GitHub pull request, and policy forces that pull request to stay a draft.

```text
Agent requests:

    github_create_pull_request(...)
              │
              ▼
        Cerbos policy
              │
              │ authorized
              ▼
      Mapping: draft = true
              │
              ▼
          GitHub API
```

A rewrite only ever applies after authorization succeeds; it can never turn a denial into an allow. This is a different kind of control from a line in the system prompt reading *"never create a non-draft pull request"* — that one depends on the agent choosing to comply.

## What a denial actually looks like

Policies return their reason to the agent. These are the real outputs from [`charts/cerbos-policies/policies/resource_github.yaml`](charts/cerbos-policies/policies/resource_github.yaml):

```text
GitHub repo acme/infrastructure is outside the allowed repo list for this agent.

Direct writes to protected branch 'main' are not allowed.
Use a feature branch and open a pull/merge request.

This agent is not allowed to set reviewers on a pull request.
Request reviews manually instead.
```

Explaining the denial is deliberate. A generic "access denied" tells an agent nothing about whether to try a different approach or abandon the goal, so it burns retries rediscovering the boundary by trial and error. Rules carry a specific message; the fallback string in the shim exists only for rules that haven't been given one yet.

## What happens if the agent is compromised

Vicegerent doesn't claim an AI agent can't be compromised. The useful question is what the agent can still do afterward.

Take a prompt injection that tells an agent to hunt for secrets, reach an internal service, and tamper with a repository. Each layer answers independently:

```text
Search for model or MCP service credentials
        │
        └── those credentials normally aren't there

Connect to an arbitrary internal service
        │
        └── Cilium blocks unconfigured destinations

Invoke an MCP tool it wasn't given
        │
        └── tool isn't exposed

Invoke an allowed tool against a forbidden resource
        │
        └── Cerbos denies it

Create an otherwise permitted GitHub PR
        │
        └── policy can constrain its arguments
```

The agent can still make bad decisions. What it can't do is convert those decisions into external effects that infrastructure outside the model hasn't already permitted.

## Credentials stay outside the sandbox

Model-provider credentials live in agentgateway, and for most MCP-backed integrations the authenticated service runs outside the sandbox entirely. The agent asks vMCP to perform an operation; vMCP authenticates to the external service. So the agent can complete an authorized operation without ever reading an API token, discovering an OAuth credential, or building an authenticated request itself.

```text
             Agent Sandbox
                  │
                  │ MCP request
                  ▼
             vMCP / Gateway
                  │
                  │ authenticated
                  ▼
            External Service
```

The sandbox isn't credential-free, and it would be misleading to imply otherwise. Each agent holds its own dashboard credentials and SSH key, and enabling the optional Slack integration places Slack values inside the sandbox.

Credential isolation complements the sandbox rather than replacing it. An agent that holds no credential at all can still misuse a capability that was deliberately exposed to it, which is why the network, tool-selection, and authorization layers exist alongside this one.

## A concrete example

Say you want an agent that fixes GitHub issues. It should read an issue, read the repository, change code, run tests, and open a pull request — and it should not merge that pull request, change repository settings, touch unrelated repositories, or create arbitrary GitHub resources.

Those boundaries become configuration rather than instructions. Tool selection gives it the five operations it needs. The repo allowlist keeps it inside one repository. The protected-branch rule keeps it off `main`. The draft rewrite means the pull request it opens always lands as a draft, waiting for a human, no matter what the model intended. It can do the engineering work without holding the authority to ship it.

## Not an agent framework

Vicegerent doesn't build prompts, orchestrate reasoning, or decide what an agent should do, and it isn't trying to replace Claude Code or Codex. It's the environment those tools run inside.

Claude Code, Codex, OpenCode, and Hermes all run in the same sandbox today, and that's more than a compatibility list. Because every enforcement point sits outside the harness, a harness doesn't need to know Vicegerent exists or be modified to participate — anything you can install in the sandbox inherits the same filesystem, network, capability, and authorization boundaries automatically. While agent harnesses are changing this fast, being able to swap one out without rebuilding the security model around it is worth a lot.

## From a laptop to a fleet

This repository runs Vicegerent for a single user on a local Kind cluster. It is not a multi-tenant enterprise platform and doesn't pretend to be one.

What it is, though, is a complete working model of the control pattern — Kubernetes for workload boundaries, Cilium and a proxy for egress, an MCP gateway that exposes capabilities without shipping credentials into the sandbox, and Cerbos for per-call authorization. Every piece of that is ordinary infrastructure an organization already knows how to operate in larger environments.

## Architecture

![Vicegerent architecture](./architecture.png)

```text
                         ┌─────────────────────┐
                         │      AI AGENT       │
                         │                     │
                         │ Claude / Codex /    │
                         │ OpenCode / Hermes   │
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │    Agent Sandbox    │
                         │                     │
                         │ Kubernetes          │
                         │ filesystem controls │
                         └──────────┬──────────┘
                                    │
                           Cilium egress policy
                                    │
                           ┌────────▼────────┐
                           │  Egress Proxy   │
                           └────────┬────────┘
                                    │
                           ┌────────▼────────┐
                           │  agentgateway   │
                           └────────┬────────┘
                                    ├── model request ──▶ Model provider
                                    │
                                    │ MCP request
                           ┌────────▼────────┐
                           │ mcp-cerbos-shim │
                           │    + Cerbos     │
                           └────────┬────────┘
                                    │
                           ┌────────▼────────┐
                           │ ToolHive / vMCP │
                           └────────┬────────┘
                                    │ authenticated
                                    ▼
                            External service
```

[`docs/design.md`](docs/design.md) has the full threat model, request paths, exact runtime and network controls, and the tradeoffs this design accepts — including how it compares to [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell), which occupies adjacent territory with a broader runtime and a different emphasis.

## Quick start

Vicegerent provisions a local Kind cluster and installs the platform through a staged, idempotent Helm workflow. You'll need macOS with Docker plus a handful of CLI tools; [`docs/setup.md`](docs/setup.md) lists every prerequisite and walks through the whole process, including credentials and troubleshooting.

```bash
# 1. Clone and enter the repository
git clone git@github.com:christensenjairus/vicegerent-agents.git
cd vicegerent-agents

# 2. Create the Kind cluster
./vicegerent setup cluster

# 3. Provision platform-wide secrets
export ANTHROPIC_API_KEY=***
./vicegerent setup secrets platform

# 4. Configure this machine, then provision each agent's secrets
cp values.example.yaml values.yaml
$EDITOR values.yaml
./vicegerent setup secrets agent <name>

# 5. Install the platform
./vicegerent install

# 6. Bring up the host-side MCP control plane
./vicegerent setup mcp
./vicegerent start

# 7. Open a shell in the agent sandbox
./vicegerent ssh <name>
```

From inside the sandbox, start whichever harness you want — `claude`, `codex`, `opencode`, or `hermes`. All four get the same containment, credentials, egress policy, and MCP access.

## Security model and limitations

Vicegerent does not make an AI agent safe. It offers no guarantee that an agent makes good decisions, writes correct code, avoids introducing vulnerabilities, understands what you meant, resists compromise, or refrains from doing damage well within the capabilities you granted it.

The goal is narrower:

> Constrain what the agent can do to external systems using enforcement points the agent doesn't control.

That comes at a real cost. Running this means running Kubernetes, Cilium, Helm, agentgateway, ToolHive, Cerbos, and an egress proxy. The complexity is the trade: you give up the simplicity of running an agent directly on your workstation and get independently enforced boundaries around unattended work in exchange. If a human is reviewing every command anyway, you probably don't need this.

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/setup.md`](docs/setup.md) | Installation, credentials, operations, troubleshooting |
| [`docs/design.md`](docs/design.md) | Architecture, threat model, and security rationale |
| [`docs/development.md`](docs/development.md) | Repository map, extension points, contributor workflow |
| [`docs/backup-and-restore.md`](docs/backup-and-restore.md) | Backup, restore, and agent-rename runbook |
| [`docs/secrets-and-pii-redaction.md`](docs/secrets-and-pii-redaction.md) | Secret and PII redaction enforcement points |
| [`AGENTS.md`](AGENTS.md) | Repository conventions for humans and agents |

## Project status

This is an actively developed infrastructure project aimed at engineers who are comfortable with Kubernetes, networking, MCP infrastructure, and authorization systems. It is not a hosted service or a turnkey application.

It exists to work on one question: how do you let increasingly capable agents do meaningful work against real systems while their authority stays constrained by infrastructure rather than by the model?

The interesting question about an AI agent is no longer whether it *can* do something. It's how much authority you can hand it without having to watch everything it does. Don't rely on a model to hold a network boundary, or on a prompt to protect a credential, or on the agent to judge which version of an otherwise-valid tool call is acceptable. Put those boundaries outside the agent.

**The agent gets capabilities. Vicegerent enforces the boundaries.**

## License

Apache-2.0. See [`LICENSE`](LICENSE).
