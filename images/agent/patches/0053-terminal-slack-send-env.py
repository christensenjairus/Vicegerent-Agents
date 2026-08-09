"""Permit the terminal's ``hermes send`` CLI to use Slack delivery credentials.

Hermes strips messaging credentials from every terminal child by default. That is
normally correct: arbitrary model-authored shell commands must not inherit bot
credentials. In this platform, however, ``hermes send`` is the supported,
operator-authorized outbound messaging entry point. It is itself a child process,
so it could not load the Slack credentials already injected into the gateway pod
and failed before reaching the proxy-bypass code.

The chart sets HERMES_TERMINAL_ALLOW_SLACK_SEND=true only for this agent image.
Forward exactly the values standalone Slack sending needs, retain all other
provider and messaging scrubbing, and explicitly scrub the signing secret (which
upstream's local-terminal scrubber currently misses). This deliberately grants a
terminal caller the ability to deliver through the configured single-operator Slack
bot; it does not expose the signing secret or unrelated provider credentials.
"""

import importlib.util
from pathlib import Path


FLAG = "HERMES_TERMINAL_ALLOW_SLACK_SEND"


def _find_module_path(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise FileNotFoundError(f"Cannot locate module: {module_name}")
    return Path(spec.origin)


def _patch_exact(path: Path, old: str, new: str, description: str) -> None:
    source = path.read_text(encoding="utf-8")
    count = source.count(old)
    if count == 1:
        path.write_text(source.replace(old, new, 1), encoding="utf-8")
        print(f"  ok  {description}")
        return
    if count == 0 and source.count(new) == 1:
        print(f"  ok  {description} (already applied)")
        return
    raise RuntimeError(
        f"Patch marker mismatch in {path}\n"
        f"  description : {description}\n"
        f"  expected    : 1 occurrence\n"
        f"  found old   : {count}\n"
        f"  found new   : {source.count(new)}"
    )


local_path = _find_module_path("tools.environments.local")
_patch_exact(
    local_path,
    old="_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()\n",
    new=(
        "_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()\n\n"
        "# Vicegerent permits its supported `hermes send` CLI to deliver through the\n"
        "# configured Slack bot. Keep the opt-in narrow: no signing secret and no\n"
        "# unrelated provider or platform credentials reach terminal children.\n"
        f"_TERMINAL_SLACK_SEND_FLAG = {FLAG!r}\n"
        "_TERMINAL_SLACK_SEND_ENV_VARS = frozenset({\n"
        "    'SLACK_BOT_TOKEN',\n"
        "    'SLACK_HOME_CHANNEL',\n"
        "})\n\n"
        "def _terminal_slack_send_enabled() -> bool:\n"
        "    return os.getenv(_TERMINAL_SLACK_SEND_FLAG, '').strip().lower() in {'1', 'true', 'yes'}\n"
    ),
    description="local.py: add narrow Slack terminal-send credential opt-in",
)
_patch_exact(
    local_path,
    old=(
        "        passthrough = _is_passthrough(key)\n"
        "        if key in _HERMES_PROVIDER_ENV_BLOCKLIST and not passthrough:\n"
        "            continue\n"
        "        resolved = _resolve_passthrough_value(key, value) if passthrough else value\n"
    ),
    new=(
        "        passthrough = _is_passthrough(key)\n"
        "        terminal_slack_send = key in _TERMINAL_SLACK_SEND_ENV_VARS and _terminal_slack_send_enabled()\n"
        "        if key == 'SLACK_SIGNING_SECRET' or (\n"
        "            key in _HERMES_PROVIDER_ENV_BLOCKLIST and not passthrough and not terminal_slack_send\n"
        "        ):\n"
        "            continue\n"
        "        resolved = _resolve_passthrough_value(key, value) if passthrough else value\n"
    ),
    description="local.py: forward only Slack send credentials and keep signing secret scrubbed",
)
_patch_exact(
    local_path,
    old=(
        "            passthrough = _is_passthrough(key)\n"
        "            if key in _HERMES_PROVIDER_ENV_BLOCKLIST and not passthrough:\n"
        "                continue\n"
        "            resolved = _resolve_passthrough_value(key, value) if passthrough else value\n"
    ),
    new=(
        "            passthrough = _is_passthrough(key)\n"
        "            terminal_slack_send = key in _TERMINAL_SLACK_SEND_ENV_VARS and _terminal_slack_send_enabled()\n"
        "            if key == 'SLACK_SIGNING_SECRET' or (\n"
        "                key in _HERMES_PROVIDER_ENV_BLOCKLIST and not passthrough and not terminal_slack_send\n"
        "            ):\n"
        "                continue\n"
        "            resolved = _resolve_passthrough_value(key, value) if passthrough else value\n"
    ),
    description="local.py: apply Slack terminal-send policy to explicit child env",
)

patched = local_path.read_text(encoding="utf-8")
for required in (
    "_TERMINAL_SLACK_SEND_FLAG",
    "_TERMINAL_SLACK_SEND_ENV_VARS",
    "_terminal_slack_send_enabled",
    "key == 'SLACK_SIGNING_SECRET'",
):
    if required not in patched:
        raise RuntimeError(f"local.py missing required terminal Slack send safeguard: {required}")

print("Patch 0053 applied and verified.")
