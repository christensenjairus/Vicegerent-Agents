#!/usr/bin/env bash
# Move state from the former shared HOME layout into Hermes' private home.
set -euo pipefail

raw_agent_home="${AGENT_HOME:-/opt/data}"
raw_hermes_home="${HERMES_HOME:-${raw_agent_home}/.hermes}"

canonical_path() {
  python3 -c 'import os.path, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

merge_missing() {
  python3 - "$1" "$2" <<'PY'
import os
import shutil
import sys
from pathlib import Path


def merge(source: Path, destination: Path) -> None:
    for entry in os.scandir(source):
        source_entry = Path(entry.path)
        destination_entry = destination / entry.name
        if destination_entry.exists() or destination_entry.is_symlink():
            if (
                entry.is_dir(follow_symlinks=False)
                and destination_entry.is_dir()
                and not destination_entry.is_symlink()
            ):
                merge(source_entry, destination_entry)
            continue
        if entry.is_symlink():
            destination_entry.symlink_to(os.readlink(source_entry))
        elif entry.is_dir(follow_symlinks=False):
            destination_entry.mkdir()
            merge(source_entry, destination_entry)
            shutil.copystat(source_entry, destination_entry, follow_symlinks=False)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(source_entry, destination_entry, follow_symlinks=False)
        else:
            raise RuntimeError(f"unsupported special file during migration: {source_entry}")


merge(Path(sys.argv[1]), Path(sys.argv[2]))
PY
}

if [[ "${raw_agent_home}" != /* ]]; then
  echo "migrate-hermes-home: AGENT_HOME must be a non-root absolute path" >&2
  exit 1
fi
agent_home="$(canonical_path "${raw_agent_home}")"
hermes_home="$(canonical_path "${raw_hermes_home}")"
if [[ "${agent_home}" == / ]]; then
  echo "migrate-hermes-home: AGENT_HOME must be a non-root absolute path" >&2
  exit 1
fi
if [[ "${hermes_home}" != "${agent_home}/.hermes" ]]; then
  echo "migrate-hermes-home: custom HERMES_HOME=${hermes_home}; migration skipped"
  exit 0
fi

mkdir -p "${hermes_home}" "${agent_home}/skills"
migration_marker="${hermes_home}/.vicegerent-home-migration-v1"
if [[ -e "${migration_marker}" || -L "${migration_marker}" ]]; then
  if [[ -f "${migration_marker}" && ! -L "${migration_marker}" ]] \
    && [[ "$(<"${migration_marker}")" == "complete" ]]; then
    echo "migrate-hermes-home: migration already completed"
    exit 0
  fi
  echo "migrate-hermes-home: invalid migration marker at ${migration_marker}" >&2
  exit 1
fi
backup_root="${hermes_home}/.legacy-home-backup-v1"

backup_source() {
  local source="$1" destination="$2" suffix=0
  while [[ -e "${destination}" || -L "${destination}" ]]; do
    suffix=$((suffix + 1))
    destination="$2.${suffix}"
  done
  mkdir -p "$(dirname "${destination}")"
  cp -a -- "${source}" "${destination}"
}

move_one() {
  local source="$1" destination
  destination="${hermes_home}/$(basename "$1")"
  if [[ ! -e "${source}" && ! -L "${source}" ]]; then
    return
  fi
  if [[ ! -e "${destination}" && ! -L "${destination}" ]]; then
    mv -- "${source}" "${destination}"
  elif [[ -d "${source}" && ! -L "${source}" && -d "${destination}" && ! -L "${destination}" ]]; then
    backup_source "${source}" "${backup_root}/root/$(basename "${source}")"
    merge_missing "${source}" "${destination}"
    rm -rf -- "${source}"
  else
    backup_source "${source}" "${backup_root}/root/$(basename "${source}")"
    rm -rf -- "${source}"
  fi
  migrated=$((migrated + 1))
}

migrated=0

# An older runtime split tool subprocess HOME under /opt/data/home. Merge the
# remaining Hermes subtree before removing that obsolete scaffold.
legacy_split_home="${agent_home}/home/.hermes"
if [[ -d "${legacy_split_home}" && ! -L "${legacy_split_home}" ]]; then
  backup_source "${legacy_split_home}" "${backup_root}/split-home"
  merge_missing "${legacy_split_home}" "${hermes_home}"
  rm -rf -- "${legacy_split_home}"
  find "${agent_home}/home" -depth -type d -empty -delete 2>/dev/null || true
  migrated=$((migrated + 1))
fi

shopt -s nullglob
for source in \
  "${agent_home}"/.anthropic_oauth.json \
  "${agent_home}"/.clean_shutdown \
  "${agent_home}"/.codex_gpt55_autoraise_notice \
  "${agent_home}"/.container-mode \
  "${agent_home}"/.drain_request.json \
  "${agent_home}"/.env \
  "${agent_home}"/.gateway-launchd-unsupported \
  "${agent_home}"/.gateway-planned-stop.json \
  "${agent_home}"/.gateway-takeover.json \
  "${agent_home}"/.hermes_history \
  "${agent_home}"/.install_method \
  "${agent_home}"/.managed \
  "${agent_home}"/.mcp-discovery.lock \
  "${agent_home}"/.no-bundled-skills \
  "${agent_home}"/.restart_last_processed.json \
  "${agent_home}"/.restart_notify.json \
  "${agent_home}"/.restart_pending.json \
  "${agent_home}"/.scratch_tip_shown \
  "${agent_home}"/.skills_prompt_snapshot.json \
  "${agent_home}"/.sync.lock \
  "${agent_home}"/.tirith-install-failed \
  "${agent_home}"/.update_check \
  "${agent_home}"/.update_exit_code \
  "${agent_home}"/.update_output.txt \
  "${agent_home}"/.update_pending.claimed.json \
  "${agent_home}"/.update_pending.json \
  "${agent_home}"/.update_prompt.json \
  "${agent_home}"/.update_response \
  "${agent_home}"/SOUL.md \
  "${agent_home}"/active_profile \
  "${agent_home}"/audio_cache \
  "${agent_home}"/auth \
  "${agent_home}"/auth.json \
  "${agent_home}"/auth.lock \
  "${agent_home}"/backups \
  "${agent_home}"/bin \
  "${agent_home}"/browser_recordings \
  "${agent_home}"/browser_screenshots \
  "${agent_home}"/byterover \
  "${agent_home}"/cache \
  "${agent_home}"/channel_aliases.json \
  "${agent_home}"/channel_directory.json \
  "${agent_home}"/checkpoints \
  "${agent_home}"/chrome-debug \
  "${agent_home}"/config.yaml \
  "${agent_home}"/config.yaml.bak-* \
  "${agent_home}"/credentials \
  "${agent_home}"/context_length_cache.yaml \
  "${agent_home}"/cron \
  "${agent_home}"/dashboard-themes \
  "${agent_home}"/delegation_cache \
  "${agent_home}"/desktop \
  "${agent_home}"/desktop-build-stamp.json \
  "${agent_home}"/desktop-plugins \
  "${agent_home}"/disk-cleanup \
  "${agent_home}"/document_cache \
  "${agent_home}"/errors.log \
  "${agent_home}"/feishu_comment_pairing.json \
  "${agent_home}"/feishu_comment_rules.json \
  "${agent_home}"/feishu_seen_message_ids.json \
  "${agent_home}"/gateway \
  "${agent_home}"/gateway-service \
  "${agent_home}"/gateway-starts.log \
  "${agent_home}"/gateway.lock \
  "${agent_home}"/gateway.pid \
  "${agent_home}"/gateway_state.json \
  "${agent_home}"/gateway_voice_mode.json \
  "${agent_home}"/google_chat_* \
  "${agent_home}"/google_client_secret.json \
  "${agent_home}"/google_oauth_pending.json \
  "${agent_home}"/google_token.json \
  "${agent_home}"/hermes-agent \
  "${agent_home}"/hermes_state.db \
  "${agent_home}"/hindsight \
  "${agent_home}"/honcho.json \
  "${agent_home}"/hook_outputs \
  "${agent_home}"/hooks \
  "${agent_home}"/image_cache \
  "${agent_home}"/images \
  "${agent_home}"/interrupt_debug.log \
  "${agent_home}"/kanban \
  "${agent_home}"/kanban.db* \
  "${agent_home}"/lazy-packages \
  "${agent_home}"/lcm-large-outputs \
  "${agent_home}"/lcm.db* \
  "${agent_home}"/lib \
  "${agent_home}"/local \
  "${agent_home}"/logs \
  "${agent_home}"/lsp \
  "${agent_home}"/mcp-installs \
  "${agent_home}"/mcp-tokens \
  "${agent_home}"/mem0.json \
  "${agent_home}"/memories \
  "${agent_home}"/memory_store.db* \
  "${agent_home}"/mnemosyne \
  "${agent_home}"/moa-traces \
  "${agent_home}"/modal_snapshots.json \
  "${agent_home}"/models_dev_cache.json \
  "${agent_home}"/node \
  "${agent_home}"/node_modules \
  "${agent_home}"/ollama_cloud_models_cache.json \
  "${agent_home}"/optional-mcps \
  "${agent_home}"/optional-skills \
  "${agent_home}"/pairing \
  "${agent_home}"/pastes \
  "${agent_home}"/pending \
  "${agent_home}"/pending_messages \
  "${agent_home}"/perf.log \
  "${agent_home}"/pets \
  "${agent_home}"/photon \
  "${agent_home}"/piper_voices_cache \
  "${agent_home}"/plans \
  "${agent_home}"/platforms \
  "${agent_home}"/plugins \
  "${agent_home}"/prefill.json \
  "${agent_home}"/processes.json \
  "${agent_home}"/profiles \
  "${agent_home}"/projects.db* \
  "${agent_home}"/provider_models_cache.json \
  "${agent_home}"/proxy \
  "${agent_home}"/retaindb_queue.db* \
  "${agent_home}"/response_store.db* \
  "${agent_home}"/runtime \
  "${agent_home}"/sandboxes \
  "${agent_home}"/scripts \
  "${agent_home}"/session-exports \
  "${agent_home}"/sessions \
  "${agent_home}"/shell-hooks-allowlist.json* \
  "${agent_home}"/singularity_snapshots.json \
  "${agent_home}"/skill-bundles \
  "${agent_home}"/skins \
  "${agent_home}"/slack-manifest.json \
  "${agent_home}"/slack_tokens.json \
  "${agent_home}"/spawn-trees \
  "${agent_home}"/state \
  "${agent_home}"/state-snapshots \
  "${agent_home}"/state.db* \
  "${agent_home}"/status_phrases \
  "${agent_home}"/status_phrases.yaml \
  "${agent_home}"/sticker_cache.json \
  "${agent_home}"/supermemory.json \
  "${agent_home}"/telemetry \
  "${agent_home}"/telephony_state.json \
  "${agent_home}"/temp_video_files \
  "${agent_home}"/temp_vision_images \
  "${agent_home}"/tmp \
  "${agent_home}"/tui-theme-boot.json \
  "${agent_home}"/unbroker \
  "${agent_home}"/verification_evidence.db* \
  "${agent_home}"/video_cache \
  "${agent_home}"/watcher-state \
  "${agent_home}"/web-ui-build-stamp.json \
  "${agent_home}"/web_cache \
  "${agent_home}"/webhook_subscriptions.json \
  "${agent_home}"/weixin \
  "${agent_home}"/whatsapp \
  "${agent_home}"/whatsapp_cloud \
  "${agent_home}"/workspace; do
  move_one "${source}"
done
shopt -u nullglob

# Shared skills are platform-owned. Hermes reaches the same canonical tree
# through a compatibility link inside its private home.
hermes_skills="${hermes_home}/skills"
if [[ -d "${hermes_skills}" && ! -L "${hermes_skills}" ]]; then
  backup_source "${hermes_skills}" "${backup_root}/hermes-skills"
  merge_missing "${hermes_skills}" "${agent_home}/skills"
  rm -rf -- "${hermes_skills}"
elif [[ -e "${hermes_skills}" || -L "${hermes_skills}" ]]; then
  rm -rf -- "${hermes_skills}"
fi
ln -s ../skills "${hermes_skills}"

marker_tmp="${migration_marker}.tmp.$$"
trap 'rm -f -- "${marker_tmp}"' EXIT
printf 'complete\n' > "${marker_tmp}"
mv -- "${marker_tmp}" "${migration_marker}"
trap - EXIT

echo "migrate-hermes-home: migrated ${migrated} legacy path(s)"
