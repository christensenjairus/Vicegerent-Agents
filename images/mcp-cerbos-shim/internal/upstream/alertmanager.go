package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// alertmanagerSilence is the subset of getSilences' JSON result this package
// needs. Alertmanager's documented v2 REST API (GET /api/v2/silences,
// GET /api/v2/silence/{id}) returns each silence as an object carrying its
// own "id" and "createdBy" fields alongside matchers/startsAt/endsAt/status --
// a well-documented, stable part of Alertmanager's public OpenAPI spec.
//
// NOTE: this field shape is inferred from Alertmanager's own documented v2
// REST API, NOT verified against a live call to mcp-alertmanager's specific
// getSilences tool (this sandbox has no Alertmanager credentials to test
// against) -- unlike linear.go's IssueTeam, which was confirmed against a
// real live response. If getSilences' actual result nests these fields
// differently (or wraps them in an envelope the way notion-search/notion-
// fetch do), SilenceCreatedBy below fails closed: a shape mismatch, a
// silence not present in the list, or a present silence with no resolvable
// createdBy all return an error rather than a silent empty/zero value. Live
// verification against a real Alertmanager instance is a mandatory follow-up
// before relying on this in production -- see the MR's own follow-up
// section.
type alertmanagerSilence struct {
	ID        string `json:"id"`
	CreatedBy string `json:"createdBy"`
}

// SilenceCreatedBy resolves a silenceID to its creator via ONE getSilences
// call, against getSilencesTool -- the caller's job to pass the SAME
// backend's own getSilences tool name (e.g. "alertmanager_gov_getSilences"
// for an alertmanager_gov-originated call), since this shim fronts more than
// one Alertmanager instance and a silence only exists in the one it actually
// belongs to (mirrors pagerduty.go's IncidentServiceID contract exactly).
//
// Unlike a single-object lookup, getSilences has no per-id variant --
// Alertmanager's MCP tool set only exposes a list-everything call (optionally
// filtered by state), so this fetches the full list and filters by id
// client-side (the same list-then-filter shape notion_author.go's
// PageAuthoredByOperator uses, for the same reason: no direct single-object
// fetch exists).
//
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error, the silence not appearing in the list, or a
// found silence with no resolvable createdBy) so the caller can fail closed.
func SilenceCreatedBy(ctx context.Context, getSilencesTool string, client ToolCaller, silenceID string) (string, error) {
	result, err := client.CallTool(ctx, getSilencesTool, map[string]any{})
	if err != nil {
		return "", fmt.Errorf("alertmanager silence owner lookup for %q: %w", silenceID, err)
	}
	var silences []alertmanagerSilence
	if err := json.Unmarshal([]byte(result.Text()), &silences); err != nil {
		return "", fmt.Errorf("alertmanager silence owner lookup for %q: malformed getSilences result: %w", silenceID, err)
	}
	for _, sil := range silences {
		if sil.ID != silenceID {
			continue
		}
		if sil.CreatedBy == "" {
			return "", fmt.Errorf("alertmanager silence owner lookup for %q: getSilences result has no resolvable createdBy", silenceID)
		}
		return sil.CreatedBy, nil
	}
	return "", fmt.Errorf("alertmanager silence owner lookup for %q: not found in getSilences result", silenceID)
}
