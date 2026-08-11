#!/bin/sh
# Vicegerent stub binary, symlinked to every CLI name in binaries.txt so an
# agent that tries the real tool gets redirected instead
# of hitting a bare "command not found" or, worse, silently succeeding if a
# real binary were ever added later. Real CLI access to this class of
# service is intentionally NOT baked into the sandbox image. Cloud-service
# CLIs are exposed through the vMCP backends declared in
# host/mcp/toolhive-servers.json; Docker builds run outside the sandbox.
name="$(basename "$0")"
if [ "$name" = docker ]; then
    echo "docker: this sandbox never runs Docker. If Docker is needed, ask the user to run it on their machine instead." >&2
    exit 127
fi

echo "'${name}': not available in this sandbox - use the vMCP server instead (find_tool / call_tool)." >&2
exit 127
