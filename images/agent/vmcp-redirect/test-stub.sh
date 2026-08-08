#!/bin/sh
set -eu

stub=$(CDPATH='' cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

cp "$stub" "$tmpdir/docker"
chmod +x "$tmpdir/docker"
if "$tmpdir/docker" >"$tmpdir/stdout" 2>"$tmpdir/stderr"; then
    echo "docker stub unexpectedly succeeded" >&2
    exit 1
fi

[ ! -s "$tmpdir/stdout" ]
[ "$(cat "$tmpdir/stderr")" = "docker: this sandbox never runs Docker. If Docker is needed, ask the user to run it on their machine instead." ]
