#!/usr/bin/env bash
# Claude Code scans its skills root ONE level deep (verified 2.1.220) but
# follows symlinks; codex/opencode recurse and read ~/.agents/skills. Hence
# a root symlink for them and a flat per-skill farm for claude -- collapsing
# the farm into one symlink hides every categorized skill from claude.
set -uo pipefail

hermes_skills="${HERMES_SKILLS_DIR:-/opt/data/skills}"
agents_root="${AGENTS_SKILLS_ROOT:-/opt/data/.agents/skills}"
claude_farm="${CLAUDE_SKILLS_FARM:-/opt/data/.claude/skills}"
# Directory name doubles as the category Hermes reports for adopted skills.
adopt_dir="${hermes_skills}/${ADOPT_CATEGORY:-harness-authored}"

[ -d "${hermes_skills}" ] || exit 0

mkdir -p "$(dirname "${agents_root}")"
if [ ! -e "${agents_root}" ] || [ -L "${agents_root}" ]; then
  ln -sfn "${hermes_skills}" "${agents_root}"
fi

mkdir -p "${claude_farm}"

# Adopt: a real dir in the farm is another harness's own skill (ours are
# always symlinks). Without this, opencode never sees it -- it runs with
# OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 -- and codex never reads that root.
adopted=0
for candidate in "${claude_farm}"/*; do
  [ -d "${candidate}" ] || continue
  [ -L "${candidate}" ] && continue
  [ -f "${candidate}/SKILL.md" ] || continue
  name="$(basename "${candidate}")"
  case "${name}" in .*) continue ;; esac
  mkdir -p "${adopt_dir}"
  link="${adopt_dir}/${name}"
  if [ -e "${link}" ] && [ ! -L "${link}" ]; then
    continue
  fi
  [ "$(readlink "${link}" 2>/dev/null || true)" = "${candidate}" ] && continue
  ln -sfn "${candidate}" "${link}" && adopted=$((adopted + 1))
done

if [ -d "${adopt_dir}" ]; then
  for link in "${adopt_dir}"/*; do
    [ -L "${link}" ] || continue
    [ -f "${link}/SKILL.md" ] || rm -f "${link}"
  done
  rmdir "${adopt_dir}" 2>/dev/null || true
fi

for link in "${claude_farm}"/*; do
  [ -L "${link}" ] || continue
  target="$(readlink -f "${link}" 2>/dev/null || true)"
  keep=no
  case "${target}" in
    "${hermes_skills}"/*) [ -f "${target}/SKILL.md" ] && keep=yes ;;
  esac
  [ "${keep}" = yes ] || rm -f "${link}"
done

linked=0
skipped=0
collisions=""
while IFS= read -r skill_md; do
  skill_dir="$(dirname "${skill_md}")"
  name="$(basename "${skill_dir}")"
  case "${name}" in .*) continue ;; esac
  link="${claude_farm}/${name}"
  # A real dir here belongs to whoever created it; never overwrite.
  if [ -e "${link}" ] && [ ! -L "${link}" ]; then
    skipped=$((skipped + 1))
    collisions="${collisions} ${name}"
    continue
  fi
  [ "$(readlink "${link}" 2>/dev/null || true)" = "${skill_dir}" ] && continue
  ln -sfn "${skill_dir}" "${link}" && linked=$((linked + 1))
done < <(find "${hermes_skills}" \
  \( -type d \( -path "${hermes_skills}/.*" -o -name references \
                -o -name templates -o -name assets -o -name scripts \) -prune \) -o \
  \( -path "${adopt_dir}/*" -prune \) -o \
  -mindepth 2 -name SKILL.md -print 2>/dev/null)

# stdout must be JSON: this also runs as a post_tool_call hook.
echo "sync-shared-skills: ${linked} published, ${adopted} adopted, ${skipped} name(s) left to their owner" >&2
if [ -n "${collisions}" ]; then
  echo "sync-shared-skills: shadowed by a non-Hermes skill of the same name:${collisions}" >&2
fi
echo '{}'
