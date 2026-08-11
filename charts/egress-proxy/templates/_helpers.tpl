{{- define "vicegerent-egress-proxy.webhooksEnabled" -}}
{{- $enabled := false -}}
{{- $defaults := default dict .Values.agentDefaults -}}
{{- range $rawAgent := .Values.agents -}}
{{- $agent := mergeOverwrite (deepCopy $defaults) (deepCopy $rawAgent) -}}
{{- if $agent.webhooks.enabled -}}
{{- $enabled = true -}}
{{- end -}}
{{- end -}}
{{- $enabled -}}
{{- end -}}
