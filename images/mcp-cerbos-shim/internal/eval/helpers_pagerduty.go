package eval

// PagerDuty-specific helper; self-registers via init().

import (
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("pagerdutyManageAttr", pagerdutyManageAttrOption)
}

// pagerdutyManageAttrOption allowlists manage_request to exactly
// {incident_ids, status} (status limited to acknowledged/resolved) rather than
// denylisting specific fields -- a denylist missed urgency/escalation_level in
// production, and the wire field for reassignment is spelled "assignement" in
// the upstream schema despite the tool's docs saying "assignment". status's
// value still needs its own check since the key itself is allowed.
func pagerdutyManageAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("pagerdutyManageAttr",
			cel.Overload("pagerdutyManageAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					req := anyMapValue(m, "manage_request")

					outOfScope := false

					status := lookupCI(req, "status", "")
					if status != "" && status != "acknowledged" && status != "resolved" {
						outOfScope = true
					}

					for k, v := range req {
						if strings.EqualFold(k, "incident_ids") || strings.EqualFold(k, "status") {
							continue
						}
						if !isEmptyValue(v) {
							outOfScope = true
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"hasOutOfScopeChange": strconv.FormatBool(outOfScope),
					})
				}),
			),
		),
	}
}

// isEmptyValue treats nil, empty string, and zero as "not actually set" so a
// field present in the map but explicitly nulled/zeroed doesn't spuriously
// trip the deny. Any other value (including a populated struct/map for
// assignement, a positive escalation_level int, or a non-empty urgency
// string) counts as set.
func isEmptyValue(v any) bool {
	switch t := v.(type) {
	case nil:
		return true
	case string:
		return t == ""
	case int:
		return t == 0
	case int64:
		return t == 0
	case float64:
		return t == 0
	case map[string]any:
		return len(t) == 0
	}
	return false
}

// anyMapValue reads m[key] as a map[string]any, tolerating either shape CEL
// might hand back (already-native map, or a nested ref.Val map converted by
// toAnyMap upstream). Missing/wrong-typed key returns an empty map.
func anyMapValue(m map[string]any, key string) map[string]any {
	for k, v := range m {
		if !strings.EqualFold(k, key) {
			continue
		}
		if sub, ok := v.(map[string]any); ok {
			return sub
		}
	}
	return map[string]any{}
}

func caseInsensitiveGet(m map[string]any, key string) (any, bool) {
	for k, v := range m {
		if strings.EqualFold(k, key) {
			return v, true
		}
	}
	return nil, false
}
