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
import sys

state_path = os.environ["FAKE_KUBECTL_STATE"]
args = [arg for arg in sys.argv[1:] if arg not in ("--context", "test-context", "-n", "webhooks")]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}

if args[:3] == ["get", "secret", "vicegerent-webhook-secrets"]:
    if not os.path.exists(state_path):
        raise SystemExit(1)
    print(json.dumps({"data": state}))
elif args[:3] == ["create", "secret", "generic"]:
    literal = next((arg.removeprefix("--from-literal=") for arg in args if arg.startswith("--from-literal=")), None)
    if literal is None:
        print("base")
    else:
        key, value = literal.split("=", 1)
        print(json.dumps({key: value}))
elif args[:2] == ["apply", "-f"]:
    payload = sys.stdin.read().strip()
    if payload == "base":
        json.dump(state, open(state_path, "w"))
    else:
        state = {key: base64.b64encode(value.encode()).decode() for key, value in json.loads(payload).items()}
        json.dump(state, open(state_path, "w"))
elif args[:3] == ["patch", "secret", "vicegerent-webhook-secrets"]:
    patch_file = args[args.index("--patch-file") + 1]
    payload = json.load(open(patch_file))["stringData"]
    state.update({key: base64.b64encode(value.encode()).decode() for key, value in payload.items()})
    json.dump(state, open(state_path, "w"))
else:
    raise SystemExit(f"unexpected kubectl invocation: {sys.argv[1:]}")
PY
chmod +x "$tmpdir/kubectl"

# Keep the functions under test synchronized with the production script without
# executing its command-line setup flow.
eval "$(sed -n '/^kc()/,/^# ensure_velero_credentials/p' "$setup_script")"
info() { :; }
warn() { :; }
export KUBE_CONTEXT=test-context
export ASSUME_YES=1
export FAKE_KUBECTL_STATE="$tmpdir/state.json"
export PATH="$tmpdir:$PATH"

WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks first__one WEBHOOK_SECRET_FIRST__ONE 'test' 1
WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks first__two WEBHOOK_SECRET_FIRST__TWO 'test' 1
WEBHOOK_SECRET_FIRST__ONE=first WEBHOOK_SECRET_FIRST__TWO=second WEBHOOK_SECRET_SECOND__THREE=third \
  ensure_literal_secret vicegerent-webhook-secrets webhooks second__three WEBHOOK_SECRET_SECOND__THREE 'test' 1

[[ "$(jq -r 'keys | join(" ")' "$tmpdir/state.json")" == 'first__one first__two second__three' ]]
printf 'Platform Secret key reconciliation test passed.\n'
