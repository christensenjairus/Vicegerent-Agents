#!/usr/bin/env python3
"""Upgrade-transition tests for the split agent and Hermes homes."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "home-scripts"
    / "migrate-hermes-home.sh"
)


def run(agent_home: Path, hermes_home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AGENT_HOME": str(agent_home)}
    env["HERMES_HOME"] = str(hermes_home or agent_home / ".hermes")
    return subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "data"
        hermes = home / ".hermes"

        write(home / "config.yaml", "legacy-config\n")
        write(home / "sessions" / "legacy.json", "legacy-session\n")
        write(home / "sessions" / "collision.json", "legacy-loses\n")
        (home / "sessions" / "legacy-link").symlink_to("legacy.json")
        write(home / "cache" / "fastembed" / "model", "weights\n")
        write(home / "desktop" / "interrupted_turns.json", "desktop-state\n")
        write(home / "plugins" / "custom" / "plugin.py", "plugin\n")
        write(home / ".scratch_tip_shown", "tip-shown\n")
        write(home / "state.db", "database\n")
        write(home / "home" / ".hermes" / "scripts" / "legacy.sh", "legacy-script\n")
        write(home / "home" / ".hermes" / "sessions" / "collision.json", "oldest-loses\n")
        write(home / ".codex" / "config.toml", "generic-codex\n")
        write(home / "certs" / "ca-bundle.crt", "generic-cert\n")
        write(home / "skills" / "shared" / "SKILL.md", "shared\n")
        write(hermes / "sessions" / "collision.json", "new-layout-wins\n")
        write(hermes / "sessions" / "current.json", "current-session\n")
        write(hermes / "skills" / "partial" / "SKILL.md", "partial\n")
        audited_state = {
            ".drain_request.json": "drain\n",
            ".restart_notify.json": "restart\n",
            ".update_pending.json": "update\n",
            "auth/google_oauth.json": "google-auth\n",
            "browser_recordings/recording.json": "recording\n",
            "channel_aliases.json": "aliases\n",
            "gateway_voice_mode.json": "voice\n",
            "google_client_secret.json": "client-secret\n",
            "google_oauth_pending.json": "pending-oauth\n",
            "prefill.json": "prefill\n",
            "provider_models_cache.json": "models\n",
            "runtime/active.json": "runtime\n",
            "slack_tokens.json": "slack\n",
            "state-snapshots/snapshot/state.db": "snapshot\n",
            "status_phrases.yaml": "status\n",
            "status_phrases/custom.yaml": "custom-status\n",
            "telephony_state.json": "telephony\n",
            "webhook_subscriptions.json": "webhook\n",
            "weixin/accounts/account.json": "weixin\n",
            "whatsapp/session/creds.json": "whatsapp\n",
            "whatsapp_cloud/media/message.ogg": "cloud-media\n",
        }
        for relative, content in audited_state.items():
            write(home / relative, content)

        first = run(home)
        if first.returncode != 0:
            raise SystemExit(f"FAIL: migration failed:\n{first.stderr}")

        expected = {
            hermes / "config.yaml": "legacy-config\n",
            hermes / "sessions" / "legacy.json": "legacy-session\n",
            hermes / "sessions" / "collision.json": "new-layout-wins\n",
            hermes / "sessions" / "current.json": "current-session\n",
            hermes / "cache" / "fastembed" / "model": "weights\n",
            hermes / "desktop" / "interrupted_turns.json": "desktop-state\n",
            hermes / "plugins" / "custom" / "plugin.py": "plugin\n",
            hermes / ".scratch_tip_shown": "tip-shown\n",
            hermes / "state.db": "database\n",
            hermes / "scripts" / "legacy.sh": "legacy-script\n",
            home / ".codex" / "config.toml": "generic-codex\n",
            home / "certs" / "ca-bundle.crt": "generic-cert\n",
            home / "skills" / "shared" / "SKILL.md": "shared\n",
            home / "skills" / "partial" / "SKILL.md": "partial\n",
        }
        expected.update(
            {hermes / relative: content for relative, content in audited_state.items()}
        )
        for path, content in expected.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"FAIL: unexpected content at {path}")
        collision_backups = {
            hermes / ".legacy-home-backup-v1" / "root" / "sessions" / "collision.json": "legacy-loses\n",
            hermes / ".legacy-home-backup-v1" / "split-home" / "sessions" / "collision.json": "oldest-loses\n",
        }
        for path, content in collision_backups.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"FAIL: migration collision was not preserved at {path}")
        if not (hermes / "skills").is_symlink():
            raise SystemExit("FAIL: Hermes skills path is not a compatibility symlink")
        if (hermes / "skills").resolve() != (home / "skills").resolve():
            raise SystemExit("FAIL: Hermes skills link does not target the canonical tree")
        legacy_link = hermes / "sessions" / "legacy-link"
        if not legacy_link.is_symlink() or os.readlink(legacy_link) != "legacy.json":
            raise SystemExit("FAIL: relative Hermes state symlink was not preserved")
        for retired in (
            home / "config.yaml",
            home / "sessions",
            home / "cache",
            home / "desktop",
            home / "plugins",
            home / ".scratch_tip_shown",
            home / "state.db",
            home / "home" / ".hermes",
        ):
            if retired.exists():
                raise SystemExit(f"FAIL: legacy Hermes path remains: {retired}")
        for relative in audited_state:
            if (home / relative).exists():
                raise SystemExit(f"FAIL: audited legacy Hermes path remains: {relative}")

        write(home / "scripts" / "user-created-after-migration.sh", "must-stay-shared\n")
        write(home / "config.yaml", "must-stay-shared\n")
        second = run(home)
        if second.returncode != 0:
            raise SystemExit("FAIL: repeated migration is not a no-op")
        for path, content in expected.items():
            if path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"FAIL: repeated migration changed {path}")
        for path in (
            home / "scripts" / "user-created-after-migration.sh",
            home / "config.yaml",
        ):
            if not path.is_file() or path.read_text(encoding="utf-8") != "must-stay-shared\n":
                raise SystemExit(f"FAIL: repeated migration consumed shared state at {path}")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "data"
        custom = home / "custom-hermes"
        write(home / "config.yaml", "must-stay\n")
        skipped = run(home, custom)
        if skipped.returncode != 0 or "migration skipped" not in skipped.stdout:
            raise SystemExit("FAIL: custom HERMES_HOME was not skipped cleanly")
        if not (home / "config.yaml").is_file() or custom.exists():
            raise SystemExit("FAIL: custom-home skip mutated the legacy layout")

    unsafe = run(Path("/"), Path("/.hermes"))
    if unsafe.returncode == 0 or "non-root absolute path" not in unsafe.stderr:
        raise SystemExit("FAIL: root AGENT_HOME was not rejected")

    # A nonexistent leaf component, not /tmp: realpath can't resolve a symlink
    # for a path segment that doesn't exist, so this normalizes to "/" on any
    # platform. /tmp itself is a symlink on macOS (-> /private/tmp), so
    # "/tmp/.." previously normalized to "/private" there instead of "/",
    # making this check a no-op outside Linux.
    root_alias = run(Path("/vicegerent-nonexistent-root-alias-check/.."), Path("/custom-hermes-home"))
    if root_alias.returncode == 0 or "non-root absolute path" not in root_alias.stderr:
        raise SystemExit("FAIL: normalized root AGENT_HOME was not rejected")

    print("PASS: Hermes state migrates under .hermes and generic agent state stays shared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
