# Patch regression tests

Run **inside a built hermes-agent image**, not in CI: each test imports the real patched files out of the installed Hermes package, so it needs `/opt/hermes/.venv` and the upstream sources on disk. There is no GitLab CI job for them and there shouldn't be — the CI runners have no Hermes install to test against.

Run them after `make -C images/hermes image` and before pushing a rebuilt tag:

```sh
docker run --rm -v "$PWD/images/hermes/patches/tests:/tests" \
  harbor.hahomelabs.com/vicegerent/hermes-agent:<tag> \
  /opt/hermes/.venv/bin/python /tests/test_0040_custom_provider_no_think_field.py
```

`test_0044_shell_hooks_dashboard_serve.py` checks that the `dashboard`/`serve` entrypoints register shell hooks, and then actually fires one end-to-end. It parses the real gate predicate with `ast` rather than string-matching, so an upstream refactor of `_prepare_agent_startup()` fails it loudly instead of passing on a stale match.

`test_shared_skills_sync.py` is the exception to the rule above: it needs no Hermes install, only bash + python3 + PyYAML, because it extracts the sync script straight out of `charts/agent/templates/shared-skills.yaml` and runs it against throwaway fixture trees. Safe to run anywhere, including CI:

```sh
python3 images/hermes/patches/tests/test_shared_skills_sync.py
VICEGERENT_SYNC_SCRIPT=/path/to/sync.sh python3 images/hermes/patches/tests/test_shared_skills_sync.py
```

`test_provider_reasoning_overrides.py` renders `charts/agent` with `helm template` and verifies that every enabled provider with a configured model and `reasoningEffort` appears under `agent.reasoning_overrides`. It needs the repository mounted, `helm` on `PATH`, and PyYAML importable.
