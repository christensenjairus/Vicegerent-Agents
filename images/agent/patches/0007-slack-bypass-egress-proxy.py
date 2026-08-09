"""
Patch: make all Slack traffic bypass the egress MITM proxy.

In the vicegerent sandbox ALL outbound traffic is pointed at the GET-only
scrubbing egress proxy via HTTPS_PROXY. Slack must NOT go through it — the proxy scrubs ``xox*`` tokens, blocks POST, and
blocks the Socket Mode WebSocket, so any Slack call routed through it fails (and
previously surfaced as a TLS error because the proxy presents a MITM cert). The
network policy already allows slack.com directly.

slack_sdk's ``AsyncBaseClient.__init__`` auto-loads ``HTTPS_PROXY`` whenever its
``proxy`` arg is ``None`` or empty, via ``load_http_proxy_from_env()`` — which
never consults ``NO_PROXY``. So even though the Hermes Slack adapter resolves the
bypass and clears ``app.client.proxy = None``, slack_bolt rebuilds a fresh
per-request context client as ``AsyncWebClient(token=..., proxy=app.client.proxy)``
= ``AsyncWebClient(proxy=None)`` — and that ``None`` re-triggers the env lookup, so
the auth middleware's ``auth.test()`` goes back through the proxy and hangs.

Fix both proxy paths: make ``load_http_proxy_from_env`` return ``None`` so an
unset proxy means "direct", and replace generic proxy resolution in standalone
text, media, and user-ID-to-DM paths with the adapter's existing Slack-aware
resolver. Explicit per-client proxies (``AsyncWebClient(proxy="http://...")``, or
the adapter's ``_apply_slack_proxy`` with a real URL) set ``client.proxy`` to a
non-empty value and never reach this loader, so a deliberately-configured
SLACK_PROXY still works.

Remove this patch if slack_sdk ever honors NO_PROXY in load_http_proxy_from_env.
"""

import importlib.util
import os
from pathlib import Path


def _find_module_path(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise FileNotFoundError(f"Cannot locate module: {module_name}")
    return Path(spec.origin)


loader_path = _find_module_path("slack_sdk.proxy_env_variable_loader")
print(f"Patching {loader_path}")


def _patch(path: Path, old: str, new: str, description: str) -> None:
    src = path.read_text()
    count = src.count(old)
    if count == 0 and new in src:
        print(f"  ok  {description} (already applied)")
        return
    if count == 0:
        raise RuntimeError(
            f"Patch marker not found in {path}\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    if count > 1:
        raise RuntimeError(
            f"Patch marker is ambiguous in {path} ({count} matches)\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    path.write_text(src.replace(old, new, 1))
    print(f"  ok  {description}")


_patch(
    loader_path,
    old=(
        "def load_http_proxy_from_env(logger: logging.Logger = _default_logger) -> Optional[str]:\n"
        "    proxy_url = (\n"
    ),
    new=(
        "def load_http_proxy_from_env(logger: logging.Logger = _default_logger) -> Optional[str]:\n"
        "    # vicegerent: Slack must bypass the GET-only MITM egress proxy (it scrubs\n"
        "    # xox* tokens and blocks POST/WebSocket). Auto-loading HTTPS_PROXY here\n"
        "    # ignores NO_PROXY and forces every Slack client back through the proxy.\n"
        "    # Return None so an unset proxy means direct; explicit per-client proxies\n"
        "    # (proxy=\"http://...\") set client.proxy directly and never reach this loader.\n"
        "    return None\n"
        "    proxy_url = (\n"
    ),
    description="proxy_env_variable_loader.py: disable env proxy auto-detection",
)


def _patch_exact(
    path: Path, old: str, new: str, expected_count: int, description: str
) -> None:
    src = path.read_text(encoding="utf-8")
    count = src.count(old)
    if count == expected_count:
        path.write_text(src.replace(old, new), encoding="utf-8")
        print(f"  ok  {description}")
        return
    if count == 0 and src.count(new) == expected_count:
        print(f"  ok  {description} (already applied)")
        return
    raise RuntimeError(
        f"Patch marker mismatch in {path}\n"
        f"  description : {description}\n"
        f"  expected    : {expected_count} occurrence(s)\n"
        f"  found old   : {count}\n"
        f"  found new   : {src.count(new)}"
    )


adapter_path = _find_module_path("plugins.platforms.slack.adapter")
_patch_exact(
    adapter_path,
    old="        _proxy = resolve_proxy_url()\n        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)\n",
    new="        _proxy = _resolve_slack_proxy_url()\n        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)\n",
    expected_count=2,
    description="adapter.py: use Slack-aware proxy resolution for standalone aiohttp paths",
)
_patch_exact(
    adapter_path,
    old="        _apply_slack_proxy(client, resolve_proxy_url())\n",
    new="        _apply_slack_proxy(client, _resolve_slack_proxy_url())\n",
    expected_count=1,
    description="adapter.py: use Slack-aware proxy resolution for standalone media",
)

patched_adapter = adapter_path.read_text(encoding="utf-8")
for function_name in ("_resolve_slack_user_dm", "_standalone_send"):
    start = patched_adapter.index(f"async def {function_name}(")
    next_function = patched_adapter.find("\nasync def ", start + 1)
    function_source = patched_adapter[start : next_function if next_function != -1 else None]
    if "resolve_proxy_url()" in function_source:
        raise RuntimeError(f"{function_name} still directly resolves the generic proxy")
    if "_resolve_slack_proxy_url()" not in function_source:
        raise RuntimeError(f"{function_name} does not use Slack-aware proxy resolution")


# ---------------------------------------------------------------------------
# Smoke-test: the loader returns None even with HTTPS_PROXY set, and a Slack
# client built with proxy=None no longer picks the proxy back up.
# ---------------------------------------------------------------------------

print("Smoke-testing patched module...")

# 1. The loader function itself returns None even with HTTPS_PROXY set. Load the
#    patched file in isolation (this process already imported slack_sdk while
#    locating the module, which binds the pre-patch loader by name).
os.environ["HTTPS_PROXY"] = "http://smoke-test-proxy:8080"
spec = importlib.util.spec_from_file_location("_patched_slack_proxy_loader", loader_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.load_http_proxy_from_env() is None, "loader should return None after patch"
os.environ.pop("HTTPS_PROXY", None)
print("  ok  load_http_proxy_from_env() -> None with HTTPS_PROXY set")

# 2. Real client behavior, in a FRESH interpreter — only a new process re-imports
#    the patched file (binding the patched loader by name), which matches how the
#    container runs the bot. Verifying in-process here would give a false result.
import subprocess
import sys

check = (
    "import os; os.environ['HTTPS_PROXY'] = 'http://smoke-test-proxy:8080';"
    "from slack_sdk.web.async_client import AsyncWebClient;"
    "c = AsyncWebClient(token='test-token', proxy=None);"
    "assert c.proxy is None, 'proxy not bypassed: %r' % (c.proxy,);"
    "e = AsyncWebClient(token='test-token', proxy='http://explicit:8080');"
    "assert e.proxy == 'http://explicit:8080', 'explicit proxy lost: %r' % (e.proxy,);"
    "print('subprocess-ok')"
)
result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
if result.returncode != 0 or "subprocess-ok" not in result.stdout:
    raise RuntimeError(
        "Slack proxy bypass not effective at runtime:\n" + result.stdout + result.stderr
    )
print("  ok  AsyncWebClient(proxy=None) bypasses proxy; explicit proxy still honored")

print("Patch 0007 applied and verified.")
