{{/*
Shared secret/PII promptGuard for all model backends — single source, included by each backend.
Regexes are the canonical images/mcp-cerbos-shim/internal/server/secret-patterns.json, injected at
render via `helm --set-file secretPatterns=…` (the same file the shim embeds via //go:embed and the
egress-proxy scrubber renders from); `required` + the empty-list guard fail the render closed.
Custom regex, NOT agentgateway `builtins:` — those carry an unscored bare \b[0-9]{9}\b that self-rejects ordinary numeric content (rationale in MR).
action: Mask depends on the shipped agentgateway-proxy mask patch; do NOT set streaming: Enabled — v1.3.1 has no streaming-mask path, so it is a silent no-op.
*/}}
{{- define "platform.promptGuard" -}}
{{- $defs := .Values.secretPatterns | required "platform: secretPatterns unset — render with helm --set-file secretPatterns=images/mcp-cerbos-shim/internal/server/secret-patterns.json" | fromJsonArray -}}
{{- if not $defs }}{{- fail "platform: secretPatterns decoded to an empty list — refusing to render an empty promptGuard" }}{{- end -}}
promptGuard:
  request:
    - regex:
        matches:
        {{- range $defs }}
          - {{ .regex | squote }}
        {{- end }}
        action: Mask
      response:
        message: Outbound content matched a secret or PII guard pattern; matched spans were masked before forwarding.
  response:
    - regex:
        matches:
        {{- range $defs }}
          - {{ .regex | squote }}
        {{- end }}
        action: Mask
      response:
        message: Inbound content matched a secret or PII guard pattern; matched spans were masked before returning.
{{- end -}}
