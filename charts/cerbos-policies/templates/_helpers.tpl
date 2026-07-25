{{/*
Join a YAML sequence into a CEL list body: ["a", "b"] -> `"a", "b"`.
Values are stored as clean sequences; this owns the quoting/joining spliced
into the policies' `in [ ... ]` expressions. An empty/nil sequence yields the
empty string, so `[ {{ celList .x }} ]` renders `[]` (a valid empty CEL list).
*/}}
{{- define "cerbos-policies.celList" -}}
{{- $out := list -}}
{{- range . -}}
{{- $out = append $out (printf "%q" .) -}}
{{- end -}}
{{- join ", " $out -}}
{{- end -}}
