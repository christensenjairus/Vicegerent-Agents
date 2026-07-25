# Patch regression tests

Run **inside a built hermes-agent image**, not in CI: each test imports the real patched files out of the installed Hermes package, so it needs `/opt/hermes/.venv` and the upstream sources on disk. There is no GitLab CI job for them and there shouldn't be — the CI runners have no Hermes install to test against.

Run them after `make -C images/hermes image` and before pushing a rebuilt tag:

```sh
docker run --rm -v "$PWD/images/hermes/patches/tests:/tests" \
  harbor.hahomelabs.com/vicegerent/hermes-agent:<tag> \
  /opt/hermes/.venv/bin/python /tests/test_0040_custom_provider_no_think_field.py
```

`test_gpt54_fallback_reasoning.py` additionally renders `charts/agent` with `helm template`, so it needs the repo mounted and `helm` on PATH (both are already in the image):

```sh
python3 test_gpt54_fallback_reasoning.py --chart-dir charts/agent --values values.defaults.yaml
```

Its static half guards `charts/agent/templates/_helpers.tpl`'s `reasoning_overrides` block; its behavioral half replays Hermes's own resolution chain. A chart edit that drops the override fails the first half even though no patch changed.
