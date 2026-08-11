{{- define "vicegerent-webhook-listener.enabled" -}}
{{- $enabled := false -}}
{{- range $rawAgent := .Values.agents -}}
{{- $agent := mergeOverwrite (deepCopy $.Values.agentDefaults) (deepCopy $rawAgent) -}}
{{- if not (kindIs "bool" $agent.webhooks.enabled) -}}
{{- fail (printf "webhook-listener: %s webhooks.enabled must be a boolean" $agent.name) -}}
{{- end -}}
{{- if $agent.webhooks.enabled -}}
{{- $enabled = true -}}
{{- end -}}
{{- end -}}
{{- $enabled -}}
{{- end -}}
