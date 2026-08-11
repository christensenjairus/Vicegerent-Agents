#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=install/lib/providers.sh
source "$repo_root/scripts/install/lib/providers.sh"

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-provider-prompts.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

printf '%s\n' 'models:
  openai: {enabled: false}
  deepseek: {enabled: true}
  zai: {enabled: false}' > "$tmpdir/values.yaml"

if provider_enabled openai "$repo_root/values.defaults.yaml" "$tmpdir/values.yaml"; then
  printf 'OpenAI should be disabled by the selected values profile\n' >&2
  exit 1
fi
provider_enabled deepseek "$repo_root/values.defaults.yaml" "$tmpdir/values.yaml"
if provider_enabled zai "$repo_root/values.defaults.yaml" "$tmpdir/values.yaml"; then
  printf 'Z.ai should be disabled by the selected values profile\n' >&2
  exit 1
fi

printf 'Provider prompt selection tests passed.\n'
