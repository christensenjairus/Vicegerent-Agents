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

{{/*
As celList, but lowercases every entry. For allowlists matched against a
case-INSENSITIVE upstream identifier, where the policy side lowercases the
incoming value too (githubAllowedRepos: GitHub owner/repo lookups ignore case;
gitlabAllowedProjects path entries: likewise). Lowercasing BOTH sides here
means an operator can write the value however the UI shows it -- listing
`MoveWorks-EMU/k8s-manifests` matches a call sending `moveworks-emu/...` and
vice versa -- instead of silently never matching. Do NOT use for values that
are genuinely case-sensitive (Linear team UUIDs, Jira project keys).
*/}}
{{- define "cerbos-policies.celListLower" -}}
{{- $out := list -}}
{{- range . -}}
{{- $out = append $out (printf "%q" (lower .)) -}}
{{- end -}}
{{- join ", " $out -}}
{{- end -}}
