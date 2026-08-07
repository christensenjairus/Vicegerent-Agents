#!/usr/bin/env python3
"""Vicegerent patch: HERMES_HOME support for the agentburn Hermes adapter.

agentburn's default_db_path() hardcodes ~/.hermes/state.db. In the vicegerent
sandbox Hermes stores state.db at $HERMES_HOME/state.db (/opt/data/.hermes/state.db),
so the agentburn MCP server can't find the DB without this fix.

History: this patch used to ALSO inject missing model prices into
agentburn/prices.py. That half moved to 0043-model-pricing.py, which is now
the single source of truth for model prices and emits them to both price
sinks (agentburn/prices.py AND agent/usage_pricing.py) from one table -- the
two were previously maintained by hand in separate patches and had already
silently drifted. Do not re-add prices here.

Remove once agentburn upstreams HERMES_HOME support.
"""
import importlib.util
import sys


_HOME_ANCHOR = (
    'def default_db_path() -> str:\n'
    '    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'
)

_HOME_REPLACEMENT = (
    'def default_db_path() -> str:\n'
    '    _home = os.environ.get("HERMES_HOME", "").strip()\n'
    '    if _home:\n'
    '        return os.path.join(_home, "state.db")\n'
    '    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")\n'
)

_HOME_MARKER = 'HERMES_HOME", "").strip()'


def _patch_hermes_home() -> None:
    spec = importlib.util.find_spec("agentburn.adapters.hermes")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate agentburn.adapters.hermes module")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if _HOME_MARKER in src:
        print(f"patch(hermes-home): already applied to {path} -- no-op")
        return

    count = src.count(_HOME_ANCHOR)
    if count != 1:
        raise SystemExit(
            f"patch(hermes-home): expected 1 anchor in {path}, found {count} "
            "(agentburn upstream changed adapters/hermes.py -- re-verify)"
        )

    src = src.replace(_HOME_ANCHOR, _HOME_REPLACEMENT, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch(hermes-home): HERMES_HOME support added to {path}")


def main() -> int:
    _patch_hermes_home()
    return 0


if __name__ == "__main__":
    sys.exit(main())
