{{/*
Shared secret/PII promptGuard for all model backends. Requests mask every registry
entry. Entries explicitly marked modelResponse: false are omitted from responses;
all remaining response matches fail closed with Reject because streaming guards do
not support Mask in agentgateway v1.4.1.
*/}}
{{- define "platform.promptGuard" -}}
{{- $defs := .Values.secretPatterns | required "platform: secretPatterns unset — render with helm --set-file secretPatterns=images/mcp-cerbos-shim/internal/server/secret-patterns.json" | fromJsonArray -}}
{{- if not $defs }}{{- fail "platform: secretPatterns decoded to an empty list — refusing to render an empty promptGuard" }}{{- end -}}
{{- $responseDefs := list -}}
{{- range $defs -}}
{{- if not (and (hasKey . "modelResponse") (eq .modelResponse false)) -}}
{{- $responseDefs = append $responseDefs . -}}
{{- end -}}
{{- end -}}
{{- if not $responseDefs }}{{- fail "platform: secretPatterns produced an empty model-response list — refusing to render an empty response promptGuard" }}{{- end -}}
promptGuard:
  streaming: Enabled
  request:
    - regex:
        matches:
        {{- range $defs }}
          - {{ .regex | squote }}
        {{- end }}
        action: Mask
  response:
    - regex:
        matches:
        {{- range $responseDefs }}
          - {{ .regex | squote }}
        {{- end }}
        action: Reject
{{- end -}}
