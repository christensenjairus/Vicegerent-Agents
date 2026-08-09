# aws-api-mcp-server (non-blocking patch)

A thin wrapper over the upstream `public.ecr.aws/awslabs-mcp/awslabs/aws-api-mcp-server` image that fixes one bug: **the server blocks its asyncio event loop while an AWS call runs.**

## The bug

`call_aws` is declared `async def`, but it executes the AWS call (`interpret_command`, and `execute_awscli_customization` for CLI customizations — both synchronous botocore/CLI work) **directly on the event loop**. A long call — e.g. `secretsmanager list-secrets` paginating a whole account (~10s, hundreds of KB) — blocks the loop, so the server can't answer *any* MCP protocol message (`initialize`, ping) until it returns.

Upstream, that freezes the whole ToolHive vMCP. The vMCP re-aggregates every backend's capabilities on each `tools/list` under one shared deadline, with no per-backend timeout; a backend stuck mid-call misses the deadline, cancels the shared context, and every sibling's query fails — `no backends returned capabilities` — so one slow AWS call takes down every tool for every request. (This image fixes the aws backend at the source.)

## The fix

`sitecustomize.py` is placed on `PYTHONPATH` (`/opt/patch`), so Python imports it at interpreter startup — before the server's `main()`. It monkeypatches `awslabs.aws_api_mcp_server.server.call_aws_helper`, offloading the two ctx-free blocking calls (`interpret_command` / `execute_awscli_customization`) to a worker thread via `asyncio.to_thread`, while keeping all FastMCP `Context` I/O on the main loop. The server now answers protocol messages *during* a long AWS call, so it never freezes the vMCP.

The patch **copies** `call_aws_helper`'s body — a wrapper can't help, because the fix has to introduce an `await` at the blocking call sites. It is guarded: the image build explicitly imports the patch and fails when expected compatibility markers are missing or the import raises instead of publishing a known-incompatible server. No behaviour changes beyond moving the blocking work off-loop — `READ_OPERATIONS_ONLY`, security policy, consent, help, and error reporting via `ctx.error` are all preserved.

## Upstreaming

This is a genuine upstream bug (an async handler doing sync blocking work). The intent is to submit the equivalent fix (`await asyncio.to_thread(...)`) to [awslabs/mcp](https://github.com/awslabs/mcp) and drop this image once released.

## Build

```sh
make image                      # native build
make release PLATFORM=linux/arm64 TAG=<release-tag>   # build + push to Harbor
```

## On base-image bumps

Renovate tracks the base image via the `# renovate` comment on the `ARG AWS_API_MCP_VERSION` in the Dockerfile. When it bumps: rebuild, confirm the patch applies (the stderr line `patched call_aws_helper …`), re-verify the copied body against upstream if the markers changed, run `python3 test_sitecustomize.py`, and bump `TAG`.
