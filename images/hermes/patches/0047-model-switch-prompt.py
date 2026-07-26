#!/usr/bin/env python3
"""Persist a refreshed system prompt atomically with runtime identity switches.

``switch_model`` invalidates the in-memory prompt, while
``update_session_billing_route`` nulls its database snapshot to reject stale
Model/Provider headers.  The next gateway turn therefore warns, rebuilds, and
misses the prefix cache.  Rewrite only the runtime identity lines and commit
that refreshed snapshot with the billing route instead.

Fail-loud on upstream drift and idempotent.  Remove once upstream persists a
refreshed prompt atomically during runtime model switches.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = "vicegerent-patch-0047"


def replace_once(source: str, old: str, new: str, path: Path) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"patch 0047: expected exactly 1 anchor in {path}, found {count}")
    return source.replace(old, new)


def main() -> int:
    root = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
    runtime_path = root / "agent" / "agent_runtime_helpers.py"
    state_path = root / "hermes_state.py"
    runtime = runtime_path.read_text(encoding="utf-8")
    state = state_path.read_text(encoding="utf-8")

    if MARKER in runtime and MARKER in state:
        print("0047: already applied")
        return 0
    if MARKER in runtime or MARKER in state:
        raise SystemExit("patch 0047: partial prior application detected")

    runtime = replace_once(
        runtime,
        '''    # ── Invalidate cached system prompt so it rebuilds next turn ──
    agent._cached_system_prompt = None
''',
        '''    # vicegerent-patch-0047: refresh identity without discarding the stable prefix.
    switched_system_prompt = getattr(agent, "_cached_system_prompt", None)
    if isinstance(switched_system_prompt, str) and switched_system_prompt:
        from agent.chat_completion_helpers import rewrite_prompt_model_identity
        rewrite_prompt_model_identity(agent, agent.model, agent.provider)
        switched_system_prompt = agent._cached_system_prompt
    else:
        switched_system_prompt = None
    agent._cached_system_prompt = None
''',
        runtime_path,
    )
    runtime = replace_once(
        runtime,
        '''                billing_mode=getattr(agent, "api_mode", None),
            )
''',
        '''                billing_mode=getattr(agent, "api_mode", None),
                system_prompt=switched_system_prompt,
            )
            if switched_system_prompt:
                agent._cached_system_prompt = switched_system_prompt
''',
        runtime_path,
    )

    state = replace_once(
        state,
        '''        billing_mode: Optional[str] = None,
    ) -> None:
''',
        '''        billing_mode: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
''',
        state_path,
    )
    state = replace_once(
        state,
        '''        Also nulls ``system_prompt`` so the cached snapshot (which embeds a
        stale ``Model:`` / ``Provider:`` header) is rebuilt — matching the
        behavior of ``update_session_model`` (see #48173, #48248).
''',
        '''        ``system_prompt`` is the refreshed snapshot carrying the new runtime
        identity. Keeping it in this write prevents a transient NULL snapshot.
''',
        state_path,
    )
    state = replace_once(
        state,
        '''                   billing_mode = COALESCE(?, billing_mode),
                   system_prompt = NULL
                   WHERE id = ?""",
                (provider, base_url, billing_mode, session_id),
''',
        '''                   billing_mode = COALESCE(?, billing_mode),
                   system_prompt = ?
                   WHERE id = ?""",  # vicegerent-patch-0047
                (provider, base_url, billing_mode, system_prompt, session_id),
''',
        state_path,
    )

    compile(runtime, str(runtime_path), "exec")
    compile(state, str(state_path), "exec")
    runtime_path.write_text(runtime, encoding="utf-8")
    state_path.write_text(state, encoding="utf-8")
    print("0047: model switches persist refreshed system prompts atomically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
