# Patch regression tests

Run **inside a built agent image**, not in CI: most tests in this directory import the real patched files out of the installed Hermes package, so they need `/opt/hermes/.venv` and the upstream sources on disk. There is no GitLab CI job for them and there shouldn't be - the CI runners have no Hermes install to test against.

Run them after `make -C images/agent image` and before pushing a rebuilt artifact. Set `AGENT_IMAGE` to the image produced by that build:

```sh
docker run --rm -v "$PWD/images/agent/patches/tests:/tests" \
  "$AGENT_IMAGE" \
  /opt/hermes/.venv/bin/python /tests/test_0040_custom_provider_no_think_field.py
```

`test_0044_shell_hooks_dashboard_serve.py` checks that the `dashboard`/`serve` entrypoints register shell hooks, and then actually fires one end-to-end. It parses the real gate predicate with `ast` rather than string-matching, so an upstream refactor of `_prepare_agent_startup()` fails it loudly instead of passing on a stale match.

`test_0049_shell_hook_approval_refresh.py` verifies that each explicit automatic-acceptance channel refreshes approval metadata after a baked hook script changes, while manually approved hooks retain the stale mtime for human review.

`test_0042_slack_socket_mode_recovery.py` copies the real Hermes Slack adapter and pinned `slack-sdk` package into a scratch tree, applies patch 0042 twice, then verifies that hanging teardown and ping writes cannot wedge the production watchdog loops. Its `--pre-fix` mode is the required negative control against pristine Hermes; set `HERMES_SOURCE_ROOT` and `HERMES_SLACK_SDK_ROOT` to those clean inputs.

`test_0050_kanban_orchestrator_guidance.py` applies patch 0050 twice to a scratch copy and exercises the real `build_system_prompt_parts()` fallback for tool-free, dispatched-worker, and board-orchestrator sessions. Its `--pre-fix` mode is the required negative control against pristine Hermes.

`test_0051_mcp_parallel_tool_calls.py` verifies that all eight global worker slots can carry deferred calls to one opted-in MCP server, while discovery or refresh retains exclusive access. Its `--pre-fix` mode is the required negative control against unpatched Hermes.

`test_0052_agent_runtime_identity.py` runs during the image build against disposable copies of the exact pristine Hermes sources, applies prerequisite patch `0046`, and requires the first `0052` application to transform those copies before checking idempotency, generated gateway and log scripts, and source syntax. In a built image, pass `--verify-applied` to verify the baked output without pretending to exercise the transformation again.

`test_0007_slack_proxy_bypass.py` has a `--pre-fix` negative control for pristine Hermes and otherwise applies patch `0007` twice to a scratch copy of Hermes and the Slack SDK. It asserts that the SDK ignores ambient proxy variables and standalone text, media, and user-to-DM resolution use the adapter's Slack-aware proxy resolver instead of the generic resolver that ignores `NO_PROXY` without target hosts.

`test_hermes_home_migration.py` needs no Hermes install. It exercises the exact-release state inventory, one-time PVC migration, recoverable destination-wins collision handling, the obsolete split-home layout, preservation of post-migration generic state, the shared-skills compatibility link, custom-home opt-out, and rejection of a root migration target. `scripts/validate.sh` runs it in CI.

`test_shared_skills_sync.py`, `test_skills_scripts_baked.py`, `test_skills_snapshot.py`, and `test_skills_snapshot_retention.py` are the exceptions to the rule above: none needs a Hermes install, only bash + python3, because they read the in-repo shell scripts, Dockerfile, and chart templates directly. Safe to run anywhere, including CI:

```sh
python3 images/agent/patches/tests/test_shared_skills_sync.py
VICEGERENT_SYNC_SCRIPT=/path/to/sync.sh python3 images/agent/patches/tests/test_shared_skills_sync.py
python3 images/agent/patches/tests/test_skills_scripts_baked.py
python3 images/agent/patches/tests/test_skills_snapshot.py
python3 images/agent/patches/tests/test_skills_snapshot_retention.py
```

`test_provider_reasoning_overrides.py` renders `charts/agent` with `helm template` and verifies that every enabled provider with a configured model and `reasoningEffort` appears under `agent.reasoning_overrides`. It needs the repository mounted, `helm` on `PATH`, and PyYAML importable.
