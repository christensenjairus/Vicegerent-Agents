{{/*
Shared secret/PII promptGuard for all model backends. Streaming response guards
support Reject but not Mask in agentgateway v1.4.1, so response matches fail closed.
*/}}
{{- define "platform.promptGuard" -}}
{{- $defs := .Values.secretPatterns | required "platform: secretPatterns unset — render with helm --set-file secretPatterns=images/mcp-cerbos-shim/internal/server/secret-patterns.json" | fromJsonArray -}}
{{- if not $defs }}{{- fail "platform: secretPatterns decoded to an empty list — refusing to render an empty promptGuard" }}{{- end -}}
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
        {{- range $defs }}
          - {{ .regex | squote }}
        {{- end }}
        action: Reject
{{- end -}}
