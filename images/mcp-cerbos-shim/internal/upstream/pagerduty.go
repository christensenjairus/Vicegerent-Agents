package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// pagerdutyIncidentResult contains the service reference returned by
// pagerduty-mcp's get_incident tool. A missing or malformed reference causes
// IncidentServiceID to return an error so authorization fails closed.
type pagerdutyIncidentResult struct {
	Service struct {
		ID string `json:"id"`
	} `json:"service"`
}

// IncidentServiceID resolves a PagerDuty incident to its owning service with
// one get_incident call. getIncidentTool must name the lookup tool on the same
// backend as the original request because the shim fronts multiple accounts.
// The lookup tool must remain unmapped to avoid recursively authorizing the
// shim's own lookup.
//
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error, or an incident with no resolvable service
// id) so the caller can fail closed -- mirrors IssueTeam/ProjectTeams's
// contract in linear.go.
func IncidentServiceID(ctx context.Context, getIncidentTool string, client ToolCaller, incidentID string) (string, error) {
	result, err := client.CallTool(ctx, getIncidentTool, map[string]any{"incident_id": incidentID})
	if err != nil {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: %w", incidentID, err)
	}
	var parsed pagerdutyIncidentResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: malformed get_incident result: %w", incidentID, err)
	}
	if parsed.Service.ID == "" {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: get_incident result has no resolvable service id", incidentID)
	}
	return parsed.Service.ID, nil
}
