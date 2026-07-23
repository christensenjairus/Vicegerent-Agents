#!/usr/bin/env python3
"""Vicegerent patch: add a new native Hermes slash command, ``/chatter``
(dispatchable in Slack as the bang-command ``!chatter`` via the existing
``!``->``/`` rewrite in patch 0031), that toggles
``display.interim_assistant_messages`` for the CURRENT SESSION ONLY.

Context
-------
``display.interim_assistant_messages`` (gateway/display_config.py) gates
whether Hermes sends natural mid-turn "here's what I'm doing" chat messages.
Slack defaults to this being on (Tier 2 / _TIER_MEDIUM); this deployment sets
it to ``false`` globally so the bot stays quiet by default, but users
occasionally want a lightweight way to flip it on temporarily -- to peek at
what the agent is doing mid-turn -- and back off again, without editing
config.yaml or redeploying.

No native command exists for this today. The closest precedents are
``/yolo`` (tools/approval.py's module-level ``_session_yolo`` set -- a pure,
in-memory, never-persisted toggle) and ``/reasoning``
(gateway/run.py's ``_session_reasoning_overrides`` dict -- session-scoped,
listed in ``_CONVERSATION_SCOPED_STATE`` so it resets at every conversation
boundary, and optionally persistable via ``--global``). This command borrows
from both: ``/yolo``'s simplicity (no ``--global`` option, nothing ever
written to config.yaml) and ``/reasoning``'s lifecycle (listed in
``_CONVERSATION_SCOPED_STATE``, so it resets on ``/new``/``/resume``/
auto-reset/compression-exhausted-reset instead of persisting indefinitely).
Note this is deliberately NOT ``/yolo``'s lifecycle: despite living outside
``_CONVERSATION_SCOPED_STATE``, ``/yolo``'s own session state is actually
still cleared on ``/new`` too, via a separate, hardcoded call in
``_clear_conversation_scope()`` to ``_clear_session_boundary_security_state()``
-- so mirroring "not in the tuple" alone would NOT have produced a
persist-across-``/new`` toggle; it would have just been an accident of two
unrelated clearing mechanisms. This patch's new attribute is intentionally
listed in ``_CONVERSATION_SCOPED_STATE`` to get the reset-on-boundary
behavior explicitly and for the same reason ``/reasoning`` gets it.

This patch touches five places across four files:

1. ``hermes_cli/commands.py`` -- registers ``CommandDef("chatter", ...)`` in
   ``COMMAND_REGISTRY``, the single source of truth every consumer (CLI
   help, gateway dispatch, autocomplete, Slack's ``!``->``/`` bang-rewrite
   via ``is_gateway_known_command()``) derives from. This is BOTH necessary
   and sufficient for patch 0031's bang-rewrite to recognize ``!chatter`` --
   no change to patch 0031 itself is needed (``GATEWAY_KNOWN_COMMANDS``
   filters on ``cli_only``/``gateway_config_gate``, not ``gateway_only``).
   ``gateway_only=True`` because only the gateway
   (gateway/slash_commands.py::_handle_chatter_command) implements this
   command -- unlike ``/yolo``, ``/reasoning``, ``/footer``, and ``/verbose``,
   there is no matching ``elif canonical == "chatter":`` arm in cli.py.

2. ``gateway/run.py`` (``GatewayRunner.__init__``) -- adds
   ``self._session_interim_assistant_message_overrides: Dict[str, bool] = {}``,
   a plain unlocked instance dict, mirroring
   ``_session_reasoning_overrides``/``_session_model_overrides``/
   ``_session_service_tier_overrides`` (all read/written only from async
   gateway-handler coroutines -- no cross-thread contention analogous to
   tools/approval.py's ``threading.Lock()``, which guards genuinely
   different, thread-blocking approval-wait state).

3. ``gateway/run.py`` (``_CONVERSATION_SCOPED_STATE``) -- lists the new
   attribute so ``_clear_conversation_scope()`` pops it at every
   conversation boundary, exactly like ``_session_reasoning_overrides``.

4. ``gateway/run.py`` (dispatch chain) -- adds
   ``if canonical == "chatter": return await
   self._handle_chatter_command(event)`` immediately after the existing
   ``"yolo"`` arm.

5. ``gateway/run.py`` (``_run_agent_inner``'s ``interim_assistant_messages_mode``
   read site) -- layers the session override on top of the existing
   ``_display_surface_mode("interim_assistant_messages", ...)`` resolution,
   purely additively: when no override is set for the current session_key,
   ``interim_assistant_messages_mode`` is byte-for-byte identical to today.

6. ``gateway/slash_commands.py`` -- adds ``_handle_chatter_command``,
   mirroring ``_handle_yolo_command``'s structure. Unlike YOLO (which has no
   persisted default to reconcile -- it is always "off" until a session
   explicitly turns it on), ``interim_assistant_messages`` DOES have a real,
   possibly platform-tiered config default, so the toggle direction on
   first use (no session override yet) resolves the same effective current
   state ``_display_surface_mode`` would compute, via the module-level twin
   helper ``_resolve_gateway_display_bool`` -- otherwise a user whose config
   default is already "on" would have their first ``/chatter`` press
   silently produce the wrong direction from their perspective.

7. ``locales/en.yaml`` -- adds a ``gateway.chatter.*`` block alongside the
   existing ``gateway.yolo.*`` block. Located via ``agent.i18n._locales_dir()``
   -- this is a plain YAML data file, not a Python module, so it cannot be
   located via ``importlib.util.find_spec()`` the way every other patch
   target in this repo is.

This patch adds wholly new functionality with no upstream equivalent to
converge on -- there is no "upstream fixed this bug" condition to word
honestly. Remove once upstream Hermes ships an equivalent native,
per-session ``interim_assistant_messages`` toggle command; until then,
re-verify every anchor below after any upstream Hermes version bump.

Fail-loud by design: each anchor is counted independently via
``_count_or_raise``; if any anchor is absent or appears an unexpected
number of times (upstream refactored one of these files), the patch raises
and the image build fails, signalling a re-verify. Each of the four
``_patch_*`` functions below also short-circuits to a no-op via its own
marker check, so re-running this script after only some files have been
patched still correctly applies just the missing ones.
"""
import importlib.util
import sys

APPLIED_MARKER = "Vicegerent patch 0040"

# --- 1. hermes_cli/commands.py: COMMAND_REGISTRY entry ---------------------

ANCHOR_COMMAND_DEF = (
    "    CommandDef(\"yolo\", \"Toggle YOLO mode (skip all dangerous command approvals)\",\n"
    "               \"Configuration\"),\n"
)

REPLACEMENT_COMMAND_DEF = (
    "    CommandDef(\"yolo\", \"Toggle YOLO mode (skip all dangerous command approvals)\",\n"
    "               \"Configuration\"),\n"
    "    # Vicegerent patch 0040: gateway_only=True because only the gateway\n"
    "    # (gateway/slash_commands.py::_handle_chatter_command) implements this\n"
    "    # command today -- unlike /yolo, /reasoning, /footer, and /verbose, there\n"
    "    # is no matching `elif canonical == \"chatter\":` arm in cli.py yet. Flip\n"
    "    # to False if/when CLI (and/or tui_gateway/server.py, which separately\n"
    "    # reads display.interim_assistant_messages for Desktop) grows one.\n"
    "    CommandDef(\"chatter\", \"Toggle interim assistant status messages for this session only\",\n"
    "               \"Configuration\", gateway_only=True),\n"
)

# --- 2. gateway/run.py: GatewayRunner.__init__ session-override dict -------

ANCHOR_INIT_DICT = (
    "        # Per-session fast-mode overrides from /fast.\n"
    "        # Key: session_key, Value: \"priority\" or None (explicit normal).\n"
    "        self._session_service_tier_overrides: Dict[str, Optional[str]] = {}\n"
)

REPLACEMENT_INIT_DICT = (
    "        # Per-session fast-mode overrides from /fast.\n"
    "        # Key: session_key, Value: \"priority\" or None (explicit normal).\n"
    "        self._session_service_tier_overrides: Dict[str, Optional[str]] = {}\n"
    "        # Vicegerent patch 0040: per-session interim_assistant_messages\n"
    "        # override from /chatter. In-memory only (never persisted to\n"
    "        # config.yaml, no --global option). Listed in\n"
    "        # _CONVERSATION_SCOPED_STATE below so it resets at every\n"
    "        # conversation boundary, mirroring _session_reasoning_overrides'\n"
    "        # exact lifecycle. Unlocked plain dict: read/written only from\n"
    "        # async gateway-handler coroutines on the event loop, same as\n"
    "        # _session_reasoning_overrides/_session_model_overrides/\n"
    "        # _session_service_tier_overrides above -- no cross-thread access\n"
    "        # analogous to tools/approval.py's threading.Lock().\n"
    "        # Key: session_key, Value: True/False; absent = no override (falls\n"
    "        # back to config.yaml's resolved interim_assistant_messages).\n"
    "        self._session_interim_assistant_message_overrides: Dict[str, bool] = {}\n"
)

# --- 3. gateway/run.py: _CONVERSATION_SCOPED_STATE -------------------------

ANCHOR_SCOPED_STATE = (
    "    \"_session_reasoning_overrides\",\n"
    "    \"_session_service_tier_overrides\",\n"
)

REPLACEMENT_SCOPED_STATE = (
    "    \"_session_reasoning_overrides\",\n"
    "    \"_session_interim_assistant_message_overrides\",\n"
    "    \"_session_service_tier_overrides\",\n"
)

# --- 4. gateway/run.py: dispatch chain --------------------------------------

ANCHOR_DISPATCH = (
    "        if canonical == \"yolo\":\n"
    "            return await self._handle_yolo_command(event)\n"
)

REPLACEMENT_DISPATCH = (
    "        if canonical == \"yolo\":\n"
    "            return await self._handle_yolo_command(event)\n"
    "\n"
    "        if canonical == \"chatter\":\n"
    "            return await self._handle_chatter_command(event)\n"
)

# --- 5. gateway/run.py: interim_assistant_messages_mode read site ----------

ANCHOR_READ_SITE = (
    "        # Natural assistant status messages are intentionally independent from\n"
    "        # tool progress and token streaming. Users can keep tool_progress quiet\n"
    "        # in chat platforms while opting into concise mid-turn updates.\n"
    "        interim_assistant_messages_mode = _display_surface_mode(\n"
    "            \"interim_assistant_messages\",\n"
    "            default=True,\n"
    "            require_platform_override_for={Platform.MATTERMOST},\n"
    "        )\n"
    "        interim_assistant_messages_enabled = (\n"
    "            source.platform != Platform.WEBHOOK\n"
    "            and interim_assistant_messages_mode != \"off\"\n"
    "        )\n"
)

REPLACEMENT_READ_SITE = (
    "        # Natural assistant status messages are intentionally independent from\n"
    "        # tool progress and token streaming. Users can keep tool_progress quiet\n"
    "        # in chat platforms while opting into concise mid-turn updates.\n"
    "        interim_assistant_messages_mode = _display_surface_mode(\n"
    "            \"interim_assistant_messages\",\n"
    "            default=True,\n"
    "            require_platform_override_for={Platform.MATTERMOST},\n"
    "        )\n"
    "        # Vicegerent patch 0040: a session-scoped /chatter override (set by\n"
    "        # _handle_chatter_command in gateway/slash_commands.py) takes\n"
    "        # precedence over the resolved config/platform value above, mirroring\n"
    "        # every other session-override precedence layer in this file (see\n"
    "        # _session_reasoning_overrides). Purely additive: when no override is\n"
    "        # set for this session_key, interim_assistant_messages_mode is\n"
    "        # unchanged from its resolved value above.\n"
    "        _interim_session_key = session_key\n"
    "        if not _interim_session_key:\n"
    "            try:\n"
    "                _interim_session_key = self._session_key_for_source(source)\n"
    "            except Exception:\n"
    "                _interim_session_key = None\n"
    "        if _interim_session_key:\n"
    "            _interim_override = (\n"
    "                getattr(self, \"_session_interim_assistant_message_overrides\", {}) or {}\n"
    "            ).get(_interim_session_key)\n"
    "            if _interim_override is not None:\n"
    "                interim_assistant_messages_mode = \"raw\" if _interim_override else \"off\"\n"
    "        interim_assistant_messages_enabled = (\n"
    "            source.platform != Platform.WEBHOOK\n"
    "            and interim_assistant_messages_mode != \"off\"\n"
    "        )\n"
)

# --- 6. gateway/slash_commands.py: new handler -----------------------------

ANCHOR_HANDLER_INSERT = (
    "    async def _handle_verbose_command(self, event: MessageEvent) -> str:\n"
)

REPLACEMENT_HANDLER_INSERT = (
    "    async def _handle_chatter_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:\n"
    "        \"\"\"Handle /chatter -- toggle interim assistant status messages for this session only.\n"
    "\n"
    "        Session-scoped, in-memory only, never persisted to config.yaml (no\n"
    "        --global option, unlike /reasoning). Resets at conversation\n"
    "        boundaries like /reasoning's session override (see\n"
    "        _CONVERSATION_SCOPED_STATE in gateway/run.py). Unlike /yolo (always\n"
    "        \"off\" until explicitly enabled), interim_assistant_messages has a\n"
    "        real config-resolved default, so the first toggle in a session\n"
    "        resolves the same effective current state _run_agent_inner would\n"
    "        compute (via the module-level _resolve_gateway_display_bool twin of\n"
    "        its _display_surface_mode closure), so the toggle direction is\n"
    "        correct on first use.\n"
    "        \"\"\"\n"
    "        from gateway.run import (\n"
    "            _load_gateway_config,\n"
    "            _platform_config_key,\n"
    "            _resolve_gateway_display_bool,\n"
    "        )\n"
    "\n"
    "        session_key = self._session_key_for_source(event.source)\n"
    "        overrides = getattr(self, \"_session_interim_assistant_message_overrides\", {}) or {}\n"
    "        current = overrides.get(session_key)\n"
    "\n"
    "        if current is None:\n"
    "            user_config = _load_gateway_config()\n"
    "            platform_key = _platform_config_key(event.source.platform)\n"
    "            current = _resolve_gateway_display_bool(\n"
    "                user_config, platform_key, \"interim_assistant_messages\",\n"
    "                default=True,\n"
    "                platform=event.source.platform,\n"
    "                require_platform_override_for={Platform.MATTERMOST},\n"
    "            )\n"
    "\n"
    "        new_value = not current\n"
    "        if not hasattr(self, \"_session_interim_assistant_message_overrides\"):\n"
    "            self._session_interim_assistant_message_overrides = {}\n"
    "        self._session_interim_assistant_message_overrides[session_key] = new_value\n"
    "\n"
    "        if new_value:\n"
    "            return EphemeralReply(t(\"gateway.chatter.enabled\"))\n"
    "        return EphemeralReply(t(\"gateway.chatter.disabled\"))\n"
    "\n"
    "    async def _handle_verbose_command(self, event: MessageEvent) -> str:\n"
)

FIX_SLASH_COMMANDS_MARKER = "Vicegerent patch 0040 (slash_commands.py)"

# --- 7. locales/en.yaml: gateway.chatter.* block ---------------------------

ANCHOR_LOCALE = (
    "  yolo:\n"
    "    disabled:                   \"⚠️ YOLO mode **OFF** for this session — dangerous commands will require approval.\"\n"
    "    enabled:                    \"⚡ YOLO mode **ON** for this session — all commands auto-approved. Use with caution.\"\n"
)

REPLACEMENT_LOCALE = (
    "  yolo:\n"
    "    disabled:                   \"⚠️ YOLO mode **OFF** for this session — dangerous commands will require approval.\"\n"
    "    enabled:                    \"⚡ YOLO mode **ON** for this session — all commands auto-approved. Use with caution.\"\n"
    "\n"
    "  chatter:\n"
    "    disabled:                   \"🔕 Interim assistant messages **OFF** for this session — no more mid-turn status updates.\"\n"
    "    enabled:                    \"💬 Interim assistant messages **ON** for this session — mid-turn status updates enabled.\"\n"
)


def _count_or_raise(src: str, anchor: str, path: str, label: str) -> None:
    count = src.count(anchor)
    if count != 1:
        raise SystemExit(
            f"patch: expected exactly 1 {label} anchor in {path}, "
            f"found {count} (upstream drifted -- re-verify)"
        )


def _patch_commands_registry() -> None:
    spec = importlib.util.find_spec("hermes_cli.commands")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate hermes_cli/commands.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_COMMAND_DEF, path, "yolo CommandDef entry")
    src = src.replace(ANCHOR_COMMAND_DEF, REPLACEMENT_COMMAND_DEF, 1)
    src += f"\n# {APPLIED_MARKER}: registered CommandDef('chatter', ...).\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: registered /chatter in COMMAND_REGISTRY in {path}")


def _patch_gateway_run() -> None:
    spec = importlib.util.find_spec("gateway.run")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/run.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_INIT_DICT, path, "__init__ session-override dict block")
    src = src.replace(ANCHOR_INIT_DICT, REPLACEMENT_INIT_DICT, 1)

    _count_or_raise(src, ANCHOR_SCOPED_STATE, path, "_CONVERSATION_SCOPED_STATE tuple entries")
    src = src.replace(ANCHOR_SCOPED_STATE, REPLACEMENT_SCOPED_STATE, 1)

    _count_or_raise(src, ANCHOR_DISPATCH, path, "yolo dispatch arm")
    src = src.replace(ANCHOR_DISPATCH, REPLACEMENT_DISPATCH, 1)

    _count_or_raise(src, ANCHOR_READ_SITE, path, "interim_assistant_messages_mode read site")
    src = src.replace(ANCHOR_READ_SITE, REPLACEMENT_READ_SITE, 1)

    src += (
        f"\n# {APPLIED_MARKER}: added _session_interim_assistant_message_overrides "
        "(incl. _CONVERSATION_SCOPED_STATE entry), the 'chatter' dispatch arm, and "
        "the session-override precedence layer on interim_assistant_messages_mode.\n"
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: /chatter session-override plumbing added to {path}")


def _patch_slash_commands() -> None:
    spec = importlib.util.find_spec("gateway.slash_commands")
    if spec is None or not spec.origin:
        raise SystemExit("patch: cannot locate gateway/slash_commands.py")
    path = spec.origin

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if FIX_SLASH_COMMANDS_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_HANDLER_INSERT, path, "_handle_verbose_command def")
    src = src.replace(ANCHOR_HANDLER_INSERT, REPLACEMENT_HANDLER_INSERT, 1)

    src += f"\n# {FIX_SLASH_COMMANDS_MARKER}: added _handle_chatter_command.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    compile(src, path, "exec")
    print(f"patch: _handle_chatter_command added to {path}")


def _patch_locale_en() -> None:
    from agent.i18n import _locales_dir

    path = str(_locales_dir() / "en.yaml")

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    if APPLIED_MARKER in src:
        print(f"patch: already applied to {path} -- no-op")
        return

    _count_or_raise(src, ANCHOR_LOCALE, path, "gateway.yolo locale block")
    src = src.replace(ANCHOR_LOCALE, REPLACEMENT_LOCALE, 1)
    src += f"\n# {APPLIED_MARKER}: added gateway.chatter.* locale keys.\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    import yaml  # PyYAML is already a hermes dependency (see agent/i18n.py)
    yaml.safe_load(src)  # self-check: still valid YAML after the edit + trailing comment
    print(f"patch: gateway.chatter.* added to {path}")


def main() -> int:
    _patch_commands_registry()
    _patch_gateway_run()
    _patch_slash_commands()
    _patch_locale_en()
    return 0


if __name__ == "__main__":
    sys.exit(main())
