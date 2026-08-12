#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
setup_script="$repo_root/scripts/install/setup-secrets-platform.sh"
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-platform-secret-reconciliation.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT INT TERM

cat > "$tmpdir/kubectl" <<'PY'
#!/usr/bin/env python3
import base64
import json
import os
import re
import sys

state_path = os.environ["FAKE_KUBECTL_STATE"]
args = [arg for arg in sys.argv[1:] if arg not in ("--context", "test-context", "-n", "webhooks")]
# One entry per Secret name, so reading or writing the wrong Secret fails here
# instead of landing in a blob every name shares.
state = json.load(open(state_path)) if os.path.exists(state_path) else {}


def save():
    json.dump(state, open(state_path, "w"))


def encode(data):
    return {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}


if args[:2] == ["get", "secret"]:
    data = state.get(args[2])
    if data is None:
        raise SystemExit(1)
    output = args[args.index("-o") + 1] if "-o" in args else ""
    if output.startswith("jsonpath="):
        # Only the escaped bracket form resolves, exactly as kubectl treats it.
        # An unescaped dotted path would address a nested field instead, so a
        # regression there reads back empty rather than silently passing.
        match = re.fullmatch(r"jsonpath=\{\.data\['(.+)'\]\}", output)
        if match is None:
            raise SystemExit(f"unsupported jsonpath: {output}")
        sys.stdout.write(data.get(match.group(1).replace("\\.", "."), ""))
    else:
        print(json.dumps({"data": data}))
elif args[:3] == ["create", "secret", "generic"]:
    literal = next((arg.removeprefix("--from-literal=") for arg in args if arg.startswith("--from-literal=")), None)
    source_file = next((arg.removeprefix("--from-file=") for arg in args if arg.startswith("--from-file=")), None)
    manifest = {"name": args[3], "data": {}}
    if literal is not None:
        key, value = literal.split("=", 1)
        manifest["data"][key] = value
    elif source_file is not None:
        key, path = source_file.split("=", 1)
        manifest["data"][key] = open(path).read()
    print(json.dumps(manifest))
elif args[:2] == ["apply", "-f"]:
    manifest = json.loads(sys.stdin.read())
    # A keyless apply only has to make the Secret exist, which is how the
    # production code creates one before patching, so it keeps stored keys.
    state[manifest["name"]] = encode(manifest["data"]) or state.get(manifest["name"], {})
    save()
elif args[:2] == ["patch", "secret"]:
    patch_file = args[args.index("--patch-file") + 1]
    state.setdefault(args[2], {}).update(encode(json.load(open(patch_file))["stringData"]))
    save()
else:
    raise SystemExit(f"unexpected kubectl invocation: {sys.argv[1:]}")
PY
chmod +x "$tmpdir/kubectl"

# Keep the functions under test synchronized with the production script without
# executing its command-line setup flow.
eval "$(sed -n '/^kc()/,/^# ensure_velero_credentials/p' "$setup_script")"
# The installer preflight reads Secret keys through lib/common.sh, so pull that
# function in too rather than restating the jsonpath it builds.
eval "$(sed -n '/^require_secret_key()/,/^}/p' "$repo_root/scripts/install/lib/common.sh")"
info() { :; }
warn() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
export KUBE_CONTEXT=test-context
export ASSUME_YES=1
export FAKE_KUBECTL_STATE="$tmpdir/state.json"
export PATH="$tmpdir:$PATH"
export VALUES_FILE="$tmpdir/values.yaml"

cat > "$VALUES_FILE" <<'YAML'
webhooks:
  tunnelId: 6ff42ae2-765d-4adf-8112-31c55c1551ef
YAML

WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks first__one WEBHOOK_SECRET_FIRST__ONE 'test' 1
WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks first__two WEBHOOK_SECRET_FIRST__TWO 'test' 1
WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks second__three WEBHOOK_SECRET_SECOND__THREE 'test' 1

webhook_keys() { jq -r '."vicegerent-webhook-secrets" | keys | join(" ")' "$tmpdir/state.json"; }
stored_credentials() {
  jq -r '."vicegerent-cloudflared-credentials"."credentials.json" // ""' "$tmpdir/state.json" | base64 -d
}

[[ "$(webhook_keys)" == 'first__one first__two second__three' ]]

matching='{"AccountTag":"account","TunnelID":"6ff42ae2-765d-4adf-8112-31c55c1551ef","TunnelSecret":"secret"}' # pragma: allowlist secret
mismatched='{"AccountTag":"account","TunnelID":"11111111-1111-1111-1111-111111111111","TunnelSecret":"secret"}' # pragma: allowlist secret
replacement='{"AccountTag":"replacement","TunnelID":"6ff42ae2-765d-4adf-8112-31c55c1551ef","TunnelSecret":"new-secret"}' # pragma: allowlist secret

seed_cloudflared_credentials() {
  local credentials="$1" encoded
  encoded="$(printf '%s' "$credentials" | base64 | tr -d '\n')"
  jq --arg encoded "$encoded" \
    '."vicegerent-cloudflared-credentials" = {"credentials.json": $encoded}' \
    "$tmpdir/state.json" > "$tmpdir/state.next.json"
  mv "$tmpdir/state.next.json" "$tmpdir/state.json"
}

seed_cloudflared_credentials "$matching"
unset CLOUDFLARED_CREDENTIALS_FILE
ensure_cloudflared_credentials
[[ "$(stored_credentials)" == "$matching" ]]

seed_cloudflared_credentials "$mismatched"
if (ensure_cloudflared_credentials); then
  echo 'Existing mismatched cloudflared credentials were accepted.' >&2
  exit 1
fi

seed_cloudflared_credentials 'not-json'
if (ensure_cloudflared_credentials); then
  echo 'Existing malformed cloudflared credentials were accepted.' >&2
  exit 1
fi

printf '%s' "$replacement" > "$tmpdir/replacement.json"
export CLOUDFLARED_CREDENTIALS_FILE="$tmpdir/replacement.json"
ensure_cloudflared_credentials
[[ "$(stored_credentials)" == "$replacement" ]]

# Both Secrets live in the same namespace and only their names separate them, so
# the tunnel credentials must never have been read from or written to the shared
# webhook signing Secret.
[[ "$(webhook_keys)" == 'first__one first__two second__three' ]]

# The installer blocks the next stage on this key, and its name contains a dot,
# so the preflight lookup only finds it while the jsonpath keeps the dot escaped
# inside a bracket selector.
require_secret_key webhooks vicegerent-cloudflared-credentials credentials.json 'test'
if (require_secret_key webhooks vicegerent-cloudflared-credentials absent.json 'test'); then
  echo 'Missing Secret key was reported as present.' >&2
  exit 1
fi

printf 'Platform Secret key reconciliation test passed.\n'
