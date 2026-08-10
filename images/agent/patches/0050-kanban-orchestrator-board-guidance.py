#!/usr/bin/env python3
"""Stop injecting the Kanban WORKER protocol into tool-only (orchestrator)
Kanban sessions.

``agent_init.py`` and its ``system_prompt.py`` fallback resolve
``agent._kanban_worker_guidance`` from a single boolean:
``"kanban_show" in agent.valid_tool_names``. But ``kanban_show`` is exposed to
TWO distinct populations (``tools/kanban_tools.py``'s ``_check_kanban_mode()``):

1. dispatcher-spawned workers, ``HERMES_KANBAN_TASK`` set; and
2. orchestrator profiles that merely list ``kanban`` in their ``toolsets``
   config, with no task of their own.

Population 2 still gets the full ``KANBAN_GUIDANCE`` worker protocol, whose
step 1 is "call ``kanban_show()`` first (no args)". With no
``$HERMES_KANBAN_TASK``, that call fails with "task_id is required", and any
scheduled/cron run on such a profile reports that failure as a tooling
regression (upstream issue #68592) even when the underlying task succeeded.

Fix: split ``_kanban_worker_guidance`` into three states -- no kanban tools
(empty), dispatcher-spawned worker (``KANBAN_GUIDANCE``), and tool-only
orchestrator (new ``KANBAN_ORCHESTRATOR_GUIDANCE`` -- board-routing guidance
only, no mandatory ``kanban_show()`` first step). Mirrors upstream salvage
PR #68608 (originally #24402/co-authored by @Sora-bluesky), adapted to this
repo's ``agent/system_prompt.py`` split (main was still pre-#68608 as of this
image's Hermes checkout).

Remove once upstream ships #68608/#68619 and this repo's Hermes version pin
advances past it.
"""
import importlib.util
import sys

PROMPT_BUILDER_ANCHOR = (
    'KANBAN_GUIDANCE = (\n'
    '    "# Kanban task execution protocol\\n"\n'
)

KANBAN_ORCHESTRATOR_GUIDANCE_BLOCK = (
    '# Board-side guidance for profiles that carry the kanban toolset WITHOUT a\n'
    '# dispatched task (orchestrator profiles, and cron/scheduled agents on such\n'
    '# profiles). Deliberately excludes the worker lifecycle: its mandatory\n'
    '# "call `kanban_show()` first" step fails with "task_id is required" when\n'
    '# no $HERMES_KANBAN_TASK exists (issue #68592).\n'
    'KANBAN_ORCHESTRATOR_GUIDANCE = (\n'
    '    "# Kanban board guidance\\n"\n'
    '    "This profile carries the `kanban_*` tools for routing work on the "\n'
    '    "shared board at `~/.hermes/kanban.db`. You are NOT a dispatched "\n'
    '    "worker: no task of your own is assigned, `$HERMES_KANBAN_TASK` is "\n'
    '    "unset, and `kanban_show()` requires an explicit `task_id` argument.\\n"\n'
    '    "- **Direct work by default.** Handle ordinary interactive work and routine "\n'
    '    "coding tasks directly. This does not override applicable coding-agent or "\n'
    '    "delegation guidance for genuinely large tasks, explicit delegation requests, "\n'
    '    "or independent review. Kanban is appropriate when the user requests it, work "\n'
    '    "must survive the current session, or parallel/dependent specialist routing "\n'
    '    "materially helps.\\n"\n'
    '    "- **When using Kanban.** Use `kanban_create(title=..., "\n'
    '    "assignee=<right-profile>, parents=[...])` to fan work out to "\n'
    '    "specialist profiles, expressing dependencies via `parents=[...]`, "\n'
    '    "not prose.\\n"\n'
    '    "- **Discover profiles first.** The dispatcher SILENTLY drops a card "\n'
    '    "with an unknown assignee (it sits in `ready` forever). Ground every "\n'
    '    "assignee in a real profile (`hermes profile list`, or ask the user).\\n"\n'
    '    "- **Created cards.** Reference task ids only when captured from a "\n'
    '    "successful `kanban_create` return -- never invent or paste ids.\\n"\n'
    '    "- **Attachments.** Attach real downloadable artifacts with "\n'
    '    "`kanban_attach` / `kanban_attach_url` (25 MB cap) instead of pasting "\n'
    '    "links in comments.\\n"\n'
    '    "- Do not shell out to `hermes kanban <verb>` for board operations. "\n'
    '    "Use the `kanban_*` tools -- they work across all terminal backends.\\n"\n'
    ')\n'
    '\n'
)

AGENT_INIT_ANCHOR = (
    '    # Kanban worker/orchestrator lifecycle guidance is session-static:\n'
    '    # the dispatcher decides at spawn time whether this process is a kanban\n'
    '    # worker (kanban_show tool is present iff HERMES_KANBAN_TASK is set).\n'
    '    # Resolving the ~835-token block once here avoids re-running the\n'
    '    # membership test + reference on every system-prompt rebuild\n'
    '    # (init + each context compression).\n'
    '    from agent.prompt_builder import KANBAN_GUIDANCE\n'
    '    agent._kanban_worker_guidance = (\n'
    '        KANBAN_GUIDANCE if "kanban_show" in agent.valid_tool_names else ""\n'
    '    )\n'
)
AGENT_INIT_REPLACEMENT = (
    '    # Kanban lifecycle guidance is session-static. The kanban toolset is\n'
    '    # available to both dispatched workers and board-side orchestrators;\n'
    '    # HERMES_KANBAN_TASK distinguishes those populations once at init.\n'
    '    from agent.prompt_builder import KANBAN_GUIDANCE, KANBAN_ORCHESTRATOR_GUIDANCE\n'
    '    if "kanban_show" not in agent.valid_tool_names:\n'
    '        agent._kanban_worker_guidance = ""\n'
    '    elif os.environ.get("HERMES_KANBAN_TASK"):\n'
    '        agent._kanban_worker_guidance = KANBAN_GUIDANCE\n'
    '    else:\n'
    '        # Kanban toolset without a dispatched task: board-routing guidance\n'
    '        # only -- no worker lifecycle, no mandatory kanban_show() first step.\n'
    '        agent._kanban_worker_guidance = KANBAN_ORCHESTRATOR_GUIDANCE\n'
)

SYSTEM_PROMPT_IMPORT_ANCHOR = "    KANBAN_GUIDANCE,\n    MEMORY_GUIDANCE,\n"
SYSTEM_PROMPT_IMPORT_REPLACEMENT = (
    "    KANBAN_GUIDANCE,\n    KANBAN_ORCHESTRATOR_GUIDANCE,\n    MEMORY_GUIDANCE,\n"
)

SYSTEM_PROMPT_FALLBACK_ANCHOR = (
    '    # Kanban worker/orchestrator lifecycle - only present when the\n'
    '    # dispatcher spawned this process (kanban_show check_fn gates on\n'
    '    # HERMES_KANBAN_TASK env var). Normal chat sessions never see\n'
    '    # this block. Resolved once at __init__ (see _kanban_worker_guidance).\n'
    '    _kanban_guidance = getattr(agent, "_kanban_worker_guidance", None)\n'
    '    if _kanban_guidance:\n'
    '        tool_guidance.append(_kanban_guidance)\n'
    '    elif _kanban_guidance is None and "kanban_show" in agent.valid_tool_names:\n'
    '        # Fallback for code paths that bypass agent_init (rare).\n'
    '        tool_guidance.append(KANBAN_GUIDANCE)\n'
)
SYSTEM_PROMPT_FALLBACK_REPLACEMENT = (
    '    # Kanban guidance covers both dispatched workers and board-side\n'
    '    # orchestrators. agent_init normally resolves the population once; a\n'
    '    # None sentinel preserves the same split for paths that bypass init.\n'
    '    _kanban_guidance = getattr(agent, "_kanban_worker_guidance", None)\n'
    '    if _kanban_guidance:\n'
    '        tool_guidance.append(_kanban_guidance)\n'
    '    elif _kanban_guidance is None and "kanban_show" in agent.valid_tool_names:\n'
    '        tool_guidance.append(\n'
    '            KANBAN_GUIDANCE\n'
    '            if os.environ.get("HERMES_KANBAN_TASK")\n'
    '            else KANBAN_ORCHESTRATOR_GUIDANCE\n'
    '        )\n'
)


def _patch_module(module_name: str, edits: list[tuple[str, str]]) -> str:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise SystemExit(f"patch: cannot locate {module_name}")
    path = spec.origin
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    already_applied = all(replacement in src for _, replacement in edits)
    if already_applied:
        return path

    for anchor, replacement in edits:
        if replacement in src:
            continue
        count = src.count(anchor)
        if count != 1:
            raise SystemExit(
                f"patch: expected exactly 1 occurrence of an anchor in {path}, "
                f"found {count} (upstream refactored -- re-verify the intent): "
                f"{anchor!r}"
            )
        src = src.replace(anchor, replacement)

    compile(src, path, "exec")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return path


def main() -> int:
    pb_path = _patch_module(
        "agent.prompt_builder",
        [(PROMPT_BUILDER_ANCHOR, KANBAN_ORCHESTRATOR_GUIDANCE_BLOCK + PROMPT_BUILDER_ANCHOR)],
    )
    ai_path = _patch_module(
        "agent.agent_init", [(AGENT_INIT_ANCHOR, AGENT_INIT_REPLACEMENT)]
    )
    sp_path = _patch_module(
        "agent.system_prompt",
        [
            (SYSTEM_PROMPT_IMPORT_ANCHOR, SYSTEM_PROMPT_IMPORT_REPLACEMENT),
            (SYSTEM_PROMPT_FALLBACK_ANCHOR, SYSTEM_PROMPT_FALLBACK_REPLACEMENT),
        ],
    )
    print(
        "patch: split Kanban worker/orchestrator guidance in "
        f"{pb_path}, {ai_path}, {sp_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
