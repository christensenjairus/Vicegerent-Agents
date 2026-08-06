# Patch regression tests

Run **inside a built agent image**, not in CI: each test imports the real patched files out of the installed Hermes package, so it needs `/opt/hermes/.venv` and the upstream sources on disk. There is no GitLab CI job for them and there shouldn't be — the CI runners have no Hermes install to test against.

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

`test_shared_skills_sync.py` is the exception to the rule above: it needs no Hermes install, only bash + python3 + PyYAML, because it reads `images/agent/skills-scripts/sync-shared-skills.sh` and runs it against throwaway fixture trees. Safe to run anywhere, including CI:

```sh
python3 images/agent/patches/tests/test_shared_skills_sync.py
VICEGERENT_SYNC_SCRIPT=/path/to/sync.sh python3 images/agent/patches/tests/test_shared_skills_sync.py
```

`test_provider_reasoning_overrides.py` renders `charts/agent` with `helm template` and verifies that every enabled provider with a configured model and `reasoningEffort` appears under `agent.reasoning_overrides`. It needs the repository mounted, `helm` on `PATH`, and PyYAML importable.
