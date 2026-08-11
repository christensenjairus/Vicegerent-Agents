#!/usr/bin/env python3
"""Use the platform-neutral ``agent`` account in Hermes container scripts.

Hermes remains installed under ``/opt/hermes`` and keeps its public
``HERMES_*`` contracts. Only the Linux account that owns runtime processes is
renamed. Upstream currently hard-codes ``hermes`` in its s6 privilege drops,
ownership repair, and docker-exec shim, so changing the Dockerfile account
alone would make the image fail during startup.

Fail-loud on upstream drift and idempotent. Remove once upstream makes the
container runtime account configurable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


DOCKER_FILES = (
    "cont-init.d/015-supervise-perms",
    "cont-init.d/02-reconcile-profiles",
    "hermes-exec-shim.sh",
    "main-wrapper.sh",
    "s6-rc.d/dashboard/run",
    "stage2-hook.sh",
)

REPLACEMENTS = (
    ('HERMES_HOME="${HERMES_HOME:-/opt/data}"', 'HERMES_HOME="${HERMES_HOME:-/opt/data/.hermes}"'),
    ("HERMES_HOME:=/opt/data}}", "HERMES_HOME:=/opt/data/.hermes}}"),
    ("_HERMES_UID", "_AGENT_UID"),
    ("_HERMES_GID", "_AGENT_GID"),
    ("actual_hermes_uid", "actual_agent_uid"),
    ("tree_has_non_hermes_owner", "tree_has_non_agent_owner"),
    ("chown_hermes_tree", "chown_agent_tree"),
    ("as_hermes", "as_agent"),
    ('usermod -u "$HERMES_UID" hermes', 'usermod -u "$HERMES_UID" agent'),
    ('groupmod -o -g "$HERMES_GID" hermes', 'groupmod -o -g "$HERMES_GID" agent'),
    ('usermod -aG "$sock_group" hermes', 'usermod -aG "$sock_group" agent'),
    ('exec "$S6_SUID" hermes', 'exec "$S6_SUID" agent'),
    ("s6-setuidgid hermes", "s6-setuidgid agent"),
    ("hermes:hermes", "agent:agent"),
    ("id -u hermes", "id -u agent"),
    ("id -g hermes", "id -g agent"),
    ("id -G hermes", "id -G agent"),
    ("! -user hermes", "! -user agent"),
    ("! -group hermes", "! -group agent"),
    ("--user hermes", "--user agent"),
    ("docker exec --user hermes", "docker exec --user agent"),
    ("unprivileged hermes user", "unprivileged agent user"),
    ("unprivileged hermes runtime", "unprivileged agent runtime"),
    ("unprivileged hermes gateway", "unprivileged agent gateway"),
    ("unprivileged hermes\n", "unprivileged agent\n"),
    ("supervised hermes user", "supervised agent user"),
    ("hermes runtime user", "agent runtime user"),
    ("hermes user reads", "agent user reads"),
    ("hermes process with", "agent process with"),
    ("hermes is already a member", "agent is already a member"),
    ("so hermes can mkdir", "so agent can mkdir"),
    ("hermes user. Hosted", "agent user. Hosted"),
    ("chowned to hermes", "chowned to agent"),
    ("non-hermes UID", "non-agent UID"),
    ("runtime hermes UID", "runtime agent UID"),
    ("hermes UID are both unaffected", "agent UID are both unaffected"),
    ("directories hermes actually writes to", "directories the agent actually writes to"),
    ("managed exclusively by hermes", "managed exclusively by the agent"),
    ("need hermes\n# ownership", "need agent\n# ownership"),
    ("Owner is already hermes\n", "Owner is already agent\n"),
    ("directory is hermes-\n", "directory is agent-\n"),
    ("$sock_group hermes failed", "$sock_group agent failed"),
    ("`hermes` user", "`agent` user"),
    ("``hermes`` user", "``agent`` user"),
    ("the hermes user", "the agent user"),
    ("the hermes build UID", "the agent build UID"),
    ("the hermes UID", "the agent UID"),
    ("the hermes GID", "the agent GID"),
    ("the hermes group", "the agent group"),
    ("hermes-user", "agent-user"),
    ("hermes-writable", "agent-writable"),
    ("hermes-owned", "agent-owned"),
    ("hermes ownership", "agent ownership"),
    ("owned by hermes", "owned by agent"),
    ("remaps the hermes user", "remaps the agent user"),
    ("Changing hermes UID", "Changing agent UID"),
    ("Changing hermes GID", "Changing agent GID"),
    ("Added hermes to group", "Added agent to group"),
    ("hermes already in group", "agent already in group"),
    ("entry that includes hermes", "entry that includes agent"),
    ("dropped hermes process", "dropped agent process"),
    ("the supervised hermes process", "the supervised agent process"),
    ("to the hermes user's home", "to the agent user's home"),
    ("Drop to hermes", "Drop to agent"),
    ("Run as hermes", "Run as agent"),
    ("works for any member of the hermes group", "works for any member of the agent group"),
    ("match the hermes user", "match the agent user"),
    ("under UID 10000 leaves", "under the agent UID leaves"),
    ("as hermes BEFORE", "as agent BEFORE"),
    ("as hermes.", "as agent."),
    ("as hermes ", "as agent "),
    ("to hermes BEFORE", "to agent BEFORE"),
    ("to hermes " + chr(8212), "to agent -"),
    ("to hermes ", "to agent "),
)

FORBIDDEN_ACTIVE = (
    "s6-setuidgid hermes",
    "hermes:hermes",
    "id -u hermes",
    "id -g hermes",
    "id -G hermes",
    "! -user hermes",
    "! -group hermes",
    'usermod -u "$HERMES_UID" hermes',
    'groupmod -o -g "$HERMES_GID" hermes',
    'usermod -aG "$sock_group" hermes',
    'exec "$S6_SUID" hermes',
)


def main() -> int:
    root = Path(os.environ.get("HERMES_DOCKER_DIR", "/opt/hermes/docker"))
    service_manager = Path(
        os.environ.get(
            "HERMES_SERVICE_MANAGER", "/opt/hermes/hermes_cli/service_manager.py"
        )
    )
    docker_paths = [root / relative for relative in DOCKER_FILES]
    paths = [*docker_paths, service_manager]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("patch 0052: missing container scripts: " + ", ".join(missing))

    sources = {path: path.read_text(encoding="utf-8") for path in paths}
    combined = "\n".join(sources.values())
    docker_combined = "\n".join(sources[path] for path in docker_paths)
    manager_source = sources[service_manager]
    remaining = [pattern for pattern in FORBIDDEN_ACTIVE if pattern in combined]
    if (
        not remaining
        and "s6-setuidgid agent" in combined
        and 'HERMES_HOME="${HERMES_HOME:-/opt/data/.hermes}"' in combined
        and "HERMES_HOME:=/opt/data/.hermes}}" in manager_source
    ):
        print("0052: already applied")
        return 0

    required = {
        "s6-setuidgid hermes": 9,
        "hermes:hermes": 12,
        "id -u hermes": 5,
        'exec "$S6_SUID" hermes': 1,
    }
    drift = [
        f"container {pattern!r}: expected {expected}, found {docker_combined.count(pattern)}"
        for pattern, expected in required.items()
        if docker_combined.count(pattern) != expected
    ]
    manager_required = {
        "s6-setuidgid hermes": 4,
        "hermes:hermes": 5,
        "HERMES_HOME:=/opt/data}}": 1,
        "_HERMES_UID": 4,
        "_HERMES_GID": 4,
    }
    drift.extend(
        f"service manager {pattern!r}: expected {expected}, found {manager_source.count(pattern)}"
        for pattern, expected in manager_required.items()
        if manager_source.count(pattern) != expected
    )
    if drift:
        raise SystemExit(
            "patch 0052: upstream runtime-account anchors drifted; " + "; ".join(drift)
        )

    changed = 0
    for path, source in sources.items():
        updated = source
        for old, new in REPLACEMENTS:
            updated = updated.replace(old, new)
        if updated != source:
            path.write_text(updated, encoding="utf-8")
            changed += 1

    patched = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    remaining = [pattern for pattern in FORBIDDEN_ACTIVE if pattern in patched]
    if remaining:
        raise SystemExit(
            "patch 0052: Hermes account references remain after patch: "
            + ", ".join(remaining)
        )
    if changed != len(paths):
        raise SystemExit(
            f"patch 0052: expected to update {len(paths)} scripts, updated {changed}"
        )

    print("0052: container runtime account renamed from hermes to agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
