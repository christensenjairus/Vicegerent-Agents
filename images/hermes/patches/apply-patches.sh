#!/bin/sh
# Apply every Vicegerent patch listed in order.txt, in listed order.
#
# Invoked from the Dockerfile's single patch-runner layer with the patches/
# directory bind-mounted read-only. Kept as a script rather than an inline RUN
# so the ordering contract and the coverage check live next to the patches they
# govern instead of being buried in shell escaping.
#
# Fails the build on: a missing file named in order.txt, a non-zero patch exit,
# or a patches/[0-9]*.py file that order.txt never mentions (the failure mode
# that would otherwise silently drop a new patch from the image).
set -eu

PATCH_DIR="${1:?usage: apply-patches.sh <patch-dir> <python>}"
PYTHON="${2:?usage: apply-patches.sh <patch-dir> <python>}"
ORDER="${PATCH_DIR}/order.txt"

[ -f "$ORDER" ] || { echo "apply-patches: missing $ORDER" >&2; exit 1; }

listed=""
count=0
while IFS= read -r line || [ -n "$line" ]; do
    name="${line%%#*}"
    name="$(printf '%s' "$name" | tr -d '[:space:]')"
    [ -n "$name" ] || continue

    if [ ! -f "${PATCH_DIR}/${name}" ]; then
        echo "apply-patches: order.txt lists ${name}, which does not exist" >&2
        exit 1
    fi

    echo "==> applying ${name}"
    "$PYTHON" "${PATCH_DIR}/${name}"

    listed="${listed} ${name}"
    count=$((count + 1))
done < "$ORDER"

# Every numbered patch must be accounted for; an unlisted one is a silent
# no-op that would ship an unpatched image.
missing=""
for path in "${PATCH_DIR}"/[0-9]*.py; do
    [ -e "$path" ] || continue
    base="$(basename "$path")"
    case " ${listed} " in
        *" ${base} "*) ;;
        *) missing="${missing} ${base}" ;;
    esac
done

if [ -n "$missing" ]; then
    echo "apply-patches: not listed in order.txt:${missing}" >&2
    echo "apply-patches: add each to order.txt at its correct apply position" >&2
    exit 1
fi

echo "apply-patches: applied ${count} patches, all patches/[0-9]*.py accounted for"
