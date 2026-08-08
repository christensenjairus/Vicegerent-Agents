package eval

// Jira-specific helper; self-registers via init().

import (
	"encoding/json"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("jiraFieldsAttr", jiraFieldsAttrOption)
}

// jiraFieldsAttrOption parses the raw-JSON `additional_fields` (create) and
// `fields`/`additional_fields` (update) args to surface fields the top-level
// schema hides: a smuggled epicKey/epic_link/parent project reference (a real
// bypass of project-scoping, not just an unmapped arg), plus assignee and
// issueType, each present at the top level only on create and only inside the
// JSON string on update. Malformed JSON yields empty attrs rather than
// failing the call, matching linearIssueAttr's fail-open-when-unverifiable
// posture (not every helper in this shim: e.g. awsSecretReadAttr and
// urlIsInternalTarget fail closed on unverifiable input).
func jiraFieldsAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("jiraFieldsAttr",
			cel.Overload("jiraFieldsAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)

					extraEpicKey := ""
					extraParentKey := ""
					// Top-level assignee/issue_type (create_issue's own args;
					// update_issue has neither top-level, only via fields JSON).
					assignee := lookupCI(m, "assignee", "")
					issueType := lookupCI(m, "issue_type", "")

					for _, argName := range []string{"additional_fields", "fields"} {
						raw := lookupCI(m, argName, "")
						if raw == "" {
							continue
						}
						var parsed map[string]any
						if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
							continue
						}
						if v := jsonStringField(parsed, "epicKey", "epic_link"); v != "" && extraEpicKey == "" {
							extraEpicKey = v
						}
						if v := jsonStringField(parsed, "parent"); v != "" && extraParentKey == "" {
							extraParentKey = v
						}
						// update_issue's assignee only ever arrives inside
						// `fields` (there's no top-level assignee arg on
						// that tool) -- only take it if we haven't already
						// found one from the top-level arg.
						if v := jsonStringField(parsed, "assignee"); v != "" && assignee == "" {
							assignee = v
						}
						// Same shape for issue_type: update_issue has no
						// top-level arg at all, only 'issuetype'/'issueType'
						// inside fields/additional_fields JSON -- either a
						// plain string or Jira's own REST-API {"name": "Epic"}
						// object shape, so check both.
						if v := jsonIssueTypeField(parsed); v != "" && issueType == "" {
							issueType = v
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"extraEpicKey":   extraEpicKey,
						"extraParentKey": extraParentKey,
						"assignee":       assignee,
						"issueType":      issueType,
					})
				}),
			),
		),
	}
}

// jsonStringField reads the first present key (case-insensitive) that holds
// a plain string value; a nested-object parent form ({"key": "OTHER-123"})
// is deliberately not unwrapped here since none of the tool's documented
// examples use that shape for parent/epicKey specifically (unlike
// priority/fixVersions, which are objects/arrays and stay unchecked --
// those don't carry a project-scoping signal).
func jsonStringField(m map[string]any, keys ...string) string {
	for k, v := range m {
		for _, want := range keys {
			if strings.EqualFold(k, want) {
				if s, ok := v.(string); ok {
					return s
				}
			}
		}
	}
	return ""
}

// jsonIssueTypeField reads an 'issuetype'/'issueType' key that's either a
// plain string, or Jira's own REST-API {"name": "Epic"} object shape --
// unlike epicKey/parent (which the tool's docs only ever show as plain
// strings), a raw fields/additional_fields JSON string smuggling an issue
// type change plausibly uses either shape, since that's the literal wire
// format Jira's REST API expects for this field.
func jsonIssueTypeField(m map[string]any) string {
	for k, v := range m {
		if !strings.EqualFold(k, "issuetype") && !strings.EqualFold(k, "issueType") {
			continue
		}
		switch t := v.(type) {
		case string:
			return t
		case map[string]any:
			if name, ok := t["name"].(string); ok {
				return name
			}
		}
	}
	return ""
}
