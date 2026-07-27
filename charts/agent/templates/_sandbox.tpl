{{- define "vicegerent-agent.sandbox" -}}
apiVersion: agents.x-k8s.io/v1beta1
kind: Sandbox
metadata:
  name: {{ include "vicegerent-agent.name" . }}
  namespace: agent-sandbox
spec:
  podTemplate:
    metadata:
      labels:
        vicegerent.io/dashboard: {{ include "vicegerent-agent.name" . }}
      annotations:
        backup.velero.io/backup-volumes-excludes: gitrepos,models,runtime,tmp,data
    spec:
      # ndots:1 so exact-matchName DNS egress (networkpolicy.yaml) works for musl (codex) —
      # see AGENTS.md DNS gotcha. Every destination here is already an FQDN.
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      automountServiceAccountToken: false
      # Map every pod UID/GID away from host IDs so a namespace escape does not
      # emerge with the same identity or capabilities on the node.
      hostUsers: false
      securityContext:
        fsGroup: 10000
        runAsUser: 10000
        runAsGroup: 10000
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: prepare-run
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          command: [sh, -c]
          args:
            - |
              set -eu
              chown 10000:10000 /run
              chown 10000:10000 /opt/data
              # chown -R: stale uid-0 dirs from old subPath design cause EPERM on reseed; idempotent on fresh PVCs.
              mkdir -p /opt/data/.codex /opt/data/.claude /opt/data/.config/opencode
              chown -R 10000:10000 /opt/data/.codex /opt/data/.claude /opt/data/.config/opencode
              # models PVC mount makes kubelet scaffold its parents as root:hermes with no group-write; fix it (non-recursive, skips the mounted content) before seed-data (uid 10000) reseeds under it.
              mkdir -p /opt/data/.hermes/mnemosyne
              chown 10000:10000 /opt/data/.hermes /opt/data/.hermes/mnemosyne
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            runAsNonRoot: false
            privileged: false
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
              add: [CHOWN, DAC_OVERRIDE]
          volumeMounts:
            - name: runtime
              mountPath: /run
            - name: data
              mountPath: /opt/data
            - name: models
              mountPath: /opt/data/.hermes/mnemosyne/models
        - name: seed-data
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          command: [bash, -c]
          args:
            - |-
              set -euo pipefail
              # /opt/data is HOME everywhere: uid 10000's /etc/passwd home, the gateway's own
              # HOME, and (via terminal.home_mode=real) every tool subprocess. Pinned here too so
              # this container writes ~/.bazelrc where it is actually read (~/.gitconfig is a
              # read-only ConfigMap mount, so the agent cannot repoint core.hooksPath).
              export HOME=/opt/data
              # fastembed reads HERMES_HOME/cache; the local LLM reads ~/.hermes; faster-whisper
              # reads the default HF_HUB_CACHE (~/.cache/huggingface/hub) — three different dirs.
              fastembed_dest="/opt/data/cache/fastembed"
              llm_dest="/opt/data/.hermes/mnemosyne/models"
              whisper_dest="/opt/data/.cache/huggingface/hub"
              marker_dir="/opt/data/.hermes"
              mkdir -p "${fastembed_dest}" "${llm_dest}" "${whisper_dest}" "${marker_dir}" /opt/data/plugins /opt/data/.ssh
              # Seed egress proxy CA cert so curl, pip, git, and Python requests trust it.
              mkdir -p /opt/data/certs
              # Build combined CA bundle: system CAs + proxy CA.
              # Using only the proxy CA would break direct-egress TLS (Slack, SSH).
              cat /etc/ssl/certs/ca-certificates.crt /reload/egress-proxy-ca/ca.crt \
                > /opt/data/certs/ca-bundle.crt
              # PKCS12 truststore for JAVA_TOOL_OPTIONS/Bazel below; keytool needs
              # per-cert aliases (unlike openssl pkcs12 -export) and a non-/tmp scratch
              # dir (seed-data has no /tmp mount). Digest-gated: importing 100+ certs
              # one keytool call at a time takes ~30s, so skip it unless the bundle
              # (system CAs or the proxy CA) actually changed since last boot.
              truststore=/opt/data/certs/java-cacerts.p12
              truststore_marker="${marker_dir}/.java-cacerts.sha256"
              want_truststore="$(sha256sum /opt/data/certs/ca-bundle.crt | cut -d' ' -f1)"
              if [ "$(cat "${truststore_marker}" 2>/dev/null || true)" != "${want_truststore}" ] || [ ! -s "${truststore}" ]; then
                rm -f "${truststore}"
                splitdir="/opt/data/certs/.ca-split"
                rm -rf "${splitdir}"
                mkdir -p "${splitdir}"
                awk -v dir="${splitdir}" \
                  '/BEGIN CERTIFICATE/{n++} {print > (dir "/ca-cert-" n ".pem")}' \
                  /opt/data/certs/ca-bundle.crt
                i=0
                for f in "${splitdir}"/ca-cert-*.pem; do
                  i=$((i + 1))
                  if ! out=$(keytool -importcert -noprompt -trustcacerts \
                    -alias "ca-${i}" -file "$f" \
                    -keystore "${truststore}" \
                    -storetype PKCS12 -storepass changeit 2>&1); then
                    echo "$out" >&2
                    exit 1
                  fi
                done
                rm -rf "${splitdir}"
                printf '%s\n' "${want_truststore}" > "${truststore_marker}"
              fi
              # One-time fold of the old split HOME into /opt/data. Destination wins: those
              # are the copies the gateway was already maintaining, and a fossil .gitconfig
              # under /opt/data/home was shadowing the chart-managed git identity.
              old_home=/opt/data/home
              if [ -d "${old_home}" ]; then
                for entry in "${old_home}"/* "${old_home}"/.[!.]*; do
                  [ -e "${entry}" ] || continue
                  name="$(basename "${entry}")"
                  # .hermes was only the models mountpoint scaffold; the PVC mounts at /opt/data/.hermes now.
                  if [ "${name}" != ".hermes" ]; then
                    if [ -e "/opt/data/${name}" ]; then
                      rm -rf "${entry}"
                    else
                      mv "${entry}" "/opt/data/${name}"
                    fi
                  fi
                done
                find "${old_home}" -depth -type d -empty -delete 2>/dev/null || true
              fi
              # Drop the PVC copy left by the old imperative `git config --global` seeding.
              # The agent container mounts ~/.gitconfig read-only from a ConfigMap; this
              # init container has no such mount, so the path it sees is the stale PVC file.
              rm -f /opt/data/.gitconfig
              # Bazel ignores JAVA_TOOL_OPTIONS; give it a ~/.bazelrc instead.
              printf '%s\n' \
                'startup --host_jvm_args=-Djavax.net.ssl.trustStore=/opt/data/certs/java-cacerts.p12' \
                'startup --host_jvm_args=-Djavax.net.ssl.trustStoreType=PKCS12' \
                'startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit' \
                > "${HOME}/.bazelrc"
              # Reseed when the marker is stale or any dest lost content: a
              # marker-only gate latches shut once a dest is wiped out from under it.
              # Compared against the seed's own entries, not a hardcoded model dir.
              seed="/opt/hermes/mnemosyne-seed"
              marker="${marker_dir}/.mnemosyne-seed.sha256"
              want="$(cat "${seed}.sha256"):layout=v4"
              seed_complete() {
                for entry in "$1"/* "$1"/.[!.]*; do
                  [ -e "${entry}" ] || continue
                  [ -e "$2/$(basename "${entry}")" ] || return 1
                done
              }
              reseed=no
              [ "$(cat "${marker}" 2>/dev/null || true)" = "${want}" ] || reseed=yes
              seed_complete "${seed}/mnemosyne/models" "${llm_dest}" || reseed=yes
              seed_complete "${seed}/cache/fastembed" "${fastembed_dest}" || reseed=yes
              seed_complete "${seed}/cache/faster-whisper" "${whisper_dest}" || reseed=yes
              if [ "${reseed}" = yes ]; then
                rm -rf "${fastembed_dest}" "${whisper_dest}"
                # llm_dest is a mountpoint; rm -rf on it errors EBUSY under set -e.
                find "${llm_dest}" -mindepth 1 -delete 2>/dev/null || true
                mkdir -p "${fastembed_dest}" "${llm_dest}" "${whisper_dest}"
                cp -dR "${seed}/mnemosyne/models/." "${llm_dest}/"
                cp -dR "${seed}/cache/fastembed/." "${fastembed_dest}/"
                cp -dR "${seed}/cache/faster-whisper/." "${whisper_dest}/"
                printf '%s\n' "${want}" > "${marker}"
                # Fail the pod rather than start an agent whose model can't load.
                for pair in "mnemosyne/models=${llm_dest}" \
                            "cache/fastembed=${fastembed_dest}" \
                            "cache/faster-whisper=${whisper_dest}"; do
                  if ! seed_complete "${seed}/${pair%%=*}" "${pair#*=}"; then
                    echo "seed-data: reseed of ${pair#*=} incomplete vs ${seed}/${pair%%=*}" >&2
                    exit 1
                  fi
                done
              fi
              pkg="$(/opt/hermes/.venv/bin/python -c 'import mnemosyne_hermes, os; print(os.path.dirname(mnemosyne_hermes.__file__))')"
              ln -sfn "${pkg}" /opt/data/plugins/mnemosyne
              # Reconcile project-owned subtrees exactly while preserving harness state
              # and user preferences outside those boundaries.
              reconcile_config() {
                local kind="$1" fmt="$2" pvc_file="$3" cm_file="$4"
                if [ -f "${pvc_file}" ]; then
                  /opt/hermes/.venv/bin/python \
                    /reload/config-reconciler/reconcile-config.py \
                    "${kind}" "${fmt}" "${pvc_file}" "${cm_file}" "${pvc_file}"
                else
                  cp "${cm_file}" "${pvc_file}"
                fi
              }
              mkdir -p /opt/data/.codex /opt/data/.claude /opt/data/.config/opencode
              reconcile_config codex toml /opt/data/.codex/config.toml /reload/codex-config/config.toml
              reconcile_config claude-settings json /opt/data/.claude/settings.json /reload/claude-config/settings.json
              reconcile_config claude-state json /opt/data/.claude/.claude.json /reload/claude-config/claude.json
              cp -f /reload/claude-config/CLAUDE.md /opt/data/.claude/CLAUDE.md
              reconcile_config opencode json /opt/data/.config/opencode/opencode.json /reload/opencode-config/opencode.json
              # OpenCode's documented global-rules location (opencode.ai/docs/rules); see
              # opencode-config.yaml's AGENTS.md key for the anomalyco/opencode#22020 caveat.
              cp -f /reload/opencode-config/AGENTS.md /opt/data/.config/opencode/AGENTS.md
              # kanban init: pre-create SQLite schema on PVC; || true because self-inits on first call anyway.
              mkdir -p /opt/data/tmp
              HERMES_HOME=/opt/data TMPDIR=/opt/data/tmp \
                /opt/hermes/.venv/bin/hermes kanban init || true
              # Remove any stale subPath artifact (dangling symlink or empty file from old design).
              [ ! -s /opt/data/config.yaml ] && rm -f /opt/data/config.yaml
              reconcile_config hermes yaml /opt/data/config.yaml /reload/hermes-config/config.yaml
              touch /opt/data/.restart_pending.json
              find /opt/data/skills -type d -perm 555 -exec chmod u+w {} + 2>/dev/null || true
              /usr/local/bin/sync-shared-skills.sh || true
              # Baseline snapshot before any harness can reach the tree; the
              # post_tool_call hook re-runs it after each skill_manage.
              /usr/local/bin/snapshot-skills.sh || true
          env:
            - name: PYTHONDONTWRITEBYTECODE
              value: '1'
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: data
              mountPath: /opt/data
            - name: models
              mountPath: /opt/data/.hermes/mnemosyne/models
            - name: config
              mountPath: /reload/hermes-config
              readOnly: true
            - name: config-reconciler
              mountPath: /reload/config-reconciler
              readOnly: true
            - name: codex-config
              mountPath: /reload/codex-config
              readOnly: true
            - name: claude-config
              mountPath: /reload/claude-config
              readOnly: true
            - name: opencode-config
              mountPath: /reload/opencode-config
              readOnly: true
            - name: egress-proxy-ca-cert
              mountPath: /reload/egress-proxy-ca
              readOnly: true
        # Slack is optional; once configured it is deliberately single-operator and DM-only.
        - name: validate-slack-access
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          command: [bash, -c]
          args:
            - |-
              set -euo pipefail
              configured=0
              for name in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL; do
                if [ -n "${!name:-}" ]; then
                  configured=1
                  break
                fi
              done
              [ "${configured}" -eq 1 ] || exit 0

              for name in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL; do
                [ -n "${!name:-}" ] || { echo "Slack is configured but ${name} is missing" >&2; exit 1; }
              done
              [[ "${SLACK_ALLOWED_USERS}" =~ ^[UW][A-Z0-9]+$ ]] || {
                echo "SLACK_ALLOWED_USERS must be exactly one Slack user ID" >&2
                exit 1
              }
              [[ "${SLACK_HOME_CHANNEL}" =~ ^D[A-Z0-9]+$ ]] || {
                echo "SLACK_HOME_CHANNEL must be a direct-message channel ID" >&2
                exit 1
              }
              [[ "${SLACK_ALLOW_ALL_USERS:-}" =~ ^([Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo])?$ ]] || {
                echo "SLACK_ALLOW_ALL_USERS must not broaden Slack access" >&2
                exit 1
              }
              [ -z "${GATEWAY_ALLOWED_USERS:-}" ] || {
                echo "GATEWAY_ALLOWED_USERS must be unset; use the single Slack allowlist" >&2
                exit 1
              }
              [[ "${GATEWAY_ALLOW_ALL_USERS:-}" =~ ^([Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo])?$ ]] || {
                echo "GATEWAY_ALLOW_ALL_USERS must not broaden Slack access" >&2
                exit 1
              }
          envFrom:
            - secretRef:
                name: {{ include "vicegerent-agent.name" . }}-secrets
                optional: false
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            capabilities:
              drop: [ALL]
        # Win the startup race: block until egress-proxy, agentgateway (via proxy),
        # and the vMCP route are all reachable before hermes starts, so a cold cluster
        # doesn't require a pod restart to recover.
        - name: wait-deps
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          command: [bash, -c]
          args:
            - |-
              set -u
              PROXY_HOST=egress-proxy.egress-proxy.svc.cluster.local
              PROXY_PORT=8080
              AGW=http://agentgateway-proxy.agentgateway-system.svc.cluster.local
              VMCP="${AGW}/mcp/vmcp"
              INTERVAL=3
              MAX=50  # ~150s per dependency

              # 1) egress-proxy: direct TCP connect (do NOT go through the proxy itself).
              n=0
              echo "waiting for egress-proxy (${PROXY_HOST}:${PROXY_PORT})..."
              until (exec 3<>"/dev/tcp/${PROXY_HOST}/${PROXY_PORT}") 2>/dev/null; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: timed out waiting for egress-proxy; continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "egress-proxy ready"

              # 2) agentgateway THROUGH the proxy: any HTTP response means it's up.
              n=0
              echo "waiting for agentgateway (via egress-proxy)..."
              until code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${AGW}/" 2>/dev/null) \
                    && [ "${code}" != "000" ]; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: timed out waiting for agentgateway (last=${code:-none}); continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "agentgateway ready (HTTP ${code})"

              # 3) vMCP: MCP initialize POST through the proxy must return HTTP 200.
              #    This exercises the full path: proxy -> agentgateway -> ghostunnel -> host ToolHive vMCP.
              n=0
              body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"wait-deps","version":"0"}}}'
              echo "waiting for vMCP initialize (200) at ${VMCP}..."
              until code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
                      -X POST \
                      -H "Accept: application/json, text/event-stream" \
                      -H "Content-Type: application/json" \
                      -d "${body}" \
                      "${VMCP}" 2>/dev/null) \
                    && [ "${code}" = "200" ]; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: vMCP did not return 200 (last=${code:-none}); continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "vMCP ready (HTTP ${code})"

              echo "wait-deps: dependency checks complete"
              exit 0
          env:
            - name: http_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: https_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTP_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTPS_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            # Only loopback bypasses the proxy — agentgateway hostname must egress via the proxy.
            - name: no_proxy
              value: 127.0.0.1,localhost
            - name: NO_PROXY
              value: 127.0.0.1,localhost
            - name: TMPDIR
              value: /tmp
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      containers:
        - name: {{ include "vicegerent-agent.name" . }}
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          args: [gateway]
          env:
            - name: HERMES_DASHBOARD
              value: '1'
            - name: HERMES_DASHBOARD_HOST
              value: 0.0.0.0
            - name: HERMES_DASHBOARD_PORT
              value: '9119'
            - name: HERMES_DASHBOARD_BASIC_AUTH_USERNAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-secrets
                  key: password
                  optional: false
            - name: HERMES_DASHBOARD_BASIC_AUTH_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-secrets
                  key: signing-secret
                  optional: false
            - name: SANDBOX_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: HERMES_HOME
              value: /opt/data
            # Route all HTTP(S) traffic through the GET-only MITM proxy.
            - name: http_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: https_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTP_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTPS_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            # Trust the proxy CA across all tooling that respects these env vars.
            - name: SSL_CERT_FILE
              value: /opt/data/certs/ca-bundle.crt
            - name: REQUESTS_CA_BUNDLE
              value: /opt/data/certs/ca-bundle.crt
            - name: CURL_CA_BUNDLE
              value: /opt/data/certs/ca-bundle.crt
            - name: GIT_SSL_CAINFO
              value: /opt/data/certs/ca-bundle.crt
            - name: NODE_EXTRA_CA_CERTS
              value: /opt/data/certs/ca-bundle.crt
            - name: PIP_CERT
              value: /opt/data/certs/ca-bundle.crt
            # JVMs (Bazel's embedded JDK, etc.) don't read the CA env vars above.
            - name: JAVA_TOOL_OPTIONS
              value: >-
                -Djavax.net.ssl.trustStore=/opt/data/certs/java-cacerts.p12
                -Djavax.net.ssl.trustStoreType=PKCS12
                -Djavax.net.ssl.trustStorePassword=changeit
            # BAZELISK_HOME: read-only image bake, no PVC copy needed (see MR).
            - name: BAZELISK_HOME
              value: /opt/hermes/.cache/bazelisk
            # Slack bypasses the proxy — Socket Mode + Web API require POST + WebSocket.
            # Loopback stays direct. All other destinations (agentgateway, searxng, internet)
            # flow through the scrubbing proxy so secrets are redacted before forwarding.
            - name: no_proxy
              value: 127.0.0.1,localhost,slack.com,.slack.com
            - name: NO_PROXY
              value: 127.0.0.1,localhost,slack.com,.slack.com
            - name: SEARXNG_URL
              value: http://searxng.searxng.svc.cluster.local:8080
            # Config homes on PVC (seeded by seed-data) to stay writable under readOnlyRootFilesystem.
            - name: CODEX_HOME
              value: /opt/data/.codex
            - name: CLAUDE_CONFIG_DIR
              value: /opt/data/.claude
            - name: OPENCODE_CONFIG
              value: /opt/data/.config/opencode/opencode.json
            # Same tree as ~/.agents/skills; without this every shared skill warns.
            - name: OPENCODE_DISABLE_CLAUDE_CODE_SKILLS
              value: '1'
            - name: XDG_CONFIG_HOME
              value: /opt/data/.config
            - name: TMPDIR
              value: /tmp
            - name: PYTHONDONTWRITEBYTECODE
              value: '1'
            - name: GIT_SSH_COMMAND
              value: ssh -i /opt/hermes-ssh/hermes_agent_ed25519 -o StrictHostKeyChecking=accept-new
                -o UserKnownHostsFile=/opt/data/.ssh/known_hosts
            # Must be "none" — Hermes's has_usable_secret() placeholder allowlist, not
            # "unused" — else canonical anthropic/openai-api falsely register as
            # user-configured and leak into the desktop model picker. Gated per-provider
            # so a disabled provider gets no env var at all.
{{- $providerCatalog := include "vicegerent-agent.providerCatalog" . | fromYaml -}}
{{- $providerOrder := include "vicegerent-agent.providerOrder" . | fromJsonArray -}}
{{- range $name := $providerOrder }}
{{- $provider := index $providerCatalog $name -}}
{{- if $provider.enabled }}
            - name: {{ $provider.keyEnv }}
              value: none
            - name: {{ $provider.baseEnv }}
              value: {{ $provider.api }}
{{- end }}
{{- end }}
{{ $mnemProvider := .Values.mnemosyne.provider -}}
{{- if not (hasKey $providerCatalog $mnemProvider) }}{{- fail (printf "mnemosyne.provider %q must be one of anthropic/openai/deepseek/zai" $mnemProvider) -}}{{- end -}}
{{- $mnem := index $providerCatalog $mnemProvider -}}
{{- if not $mnem.enabled }}{{- fail (printf "mnemosyne.provider %q is disabled in values.providers" $mnemProvider) -}}{{- end }}
            - name: MNEMOSYNE_LLM_ENABLED
              value: 'true'
            - name: MNEMOSYNE_LLM_BASE_URL
              value: {{ $mnem.mnemosyneApi }}
            - name: MNEMOSYNE_LLM_MODEL
              value: {{ $mnem.mnemosyneModel }}
            - name: MNEMOSYNE_LLM_API_KEY
              value: unused
            - name: HF_HUB_OFFLINE
              value: '1'
            - name: HERMES_WRITE_SAFE_ROOT
              value: "/opt/data:/workspace:/tmp"
{{- if .Values.obsidian.vaultPath }}
            - name: OBSIDIAN_VAULT_PATH
              value: {{ .Values.obsidian.vaultPath | quote }}
{{- end }}
          envFrom:
            # All agent pod credentials: dashboard auth, SSH key, and optional Slack tokens.
            - secretRef:
                name: {{ include "vicegerent-agent.name" . }}-secrets
                optional: false
          ports:
            - containerPort: 8642
              name: api
            - containerPort: 9119
              name: dashboard
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: runtime
              mountPath: /run
            - name: tmp
              mountPath: /tmp
            - name: data
              mountPath: /opt/data
            - name: models
              mountPath: /opt/data/.hermes/mnemosyne/models
            - name: gitrepos
              mountPath: /workspace
            - name: ssh-key
              mountPath: /opt/hermes-ssh
              readOnly: true
            - name: config
              mountPath: /reload/hermes-config
            - name: gitconfig
              mountPath: /opt/data/.gitconfig
              subPath: .gitconfig
              readOnly: true
            - name: soul
              mountPath: /opt/data/SOUL.md
              subPath: SOUL.md
            - name: approval-policy
              mountPath: /opt/hermes/approval-policy.yaml
              subPath: approval-policy.yaml
              readOnly: true
      volumes:
        - name: runtime
          emptyDir: {}
        - name: tmp
          emptyDir: {}
        - name: data
          persistentVolumeClaim:
            claimName: data-{{ include "vicegerent-agent.name" . }}
        - name: gitrepos
          persistentVolumeClaim:
            claimName: gitrepos-{{ include "vicegerent-agent.name" . }}
        - name: models
          persistentVolumeClaim:
            claimName: models-{{ include "vicegerent-agent.name" . }}
        - name: config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-config
        - name: config-reconciler
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-config-reconciler
        - name: gitconfig
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-gitconfig
            optional: true
        - name: soul
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-soul
        - name: approval-policy
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-approval-policy
        - name: codex-config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-codex-config
        - name: claude-config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-claude-config
        - name: opencode-config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-opencode-config
        - name: ssh-key
          secret:
            secretName: {{ include "vicegerent-agent.name" . }}-ssh-key  # pragma: allowlist secret
            defaultMode: 0400
            optional: true
        - name: egress-proxy-ca-cert
          secret:
            secretName: egress-proxy-ca-cert  # pragma: allowlist secret
            optional: false
{{- end -}}
