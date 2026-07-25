package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// linearGetIssueTool is the vMCP tool name for Linear's read-only issue
// fetch -- backend-prefixed the same way mapping.yaml keys its tools
// (linear_get_issue), since that's the name this call re-enters CheckRequest
// as. Kept unmapped in Cerbos for the same recursion-safety reason
// notion_notion-fetch is documented in client.go's package doc: a future
// deny rule on this tool would make every save_comment team lookup fail
// closed (not silently allow -- IssueTeam already returns an error on any
// lookup failure), but it would look like an unrelated regression if
// someone maps it without reading this comment first.
const linearGetIssueTool = "linear_get_issue"

// linearIssueDetails is the subset of linear_get_issue's JSON result this
// package needs. "team" carries the team's display name directly at the top
// level (verified live against the real vMCP route) -- e.g.
// {"id":"PROJ-69",...,"team":"HAHomelabs",...}. This is a single JSON
// object, NOT the double-JSON-wrapped shape Notion's notion-fetch uses (see
// ancestry.go's notionFetchEnvelope) -- Linear's own MCP server returns its
// tool result as plain JSON text, no extra nesting.
//
// "assignee" carries the assignee's DISPLAY NAME as a bare top-level string
// -- verified live 2026-07-25 against the real vMCP route, e.g.
// {..., "assignee":"Jairus Christensen", "assigneeId":"f60cb294-..."}. The
// stable user UUID rides in a SEPARATE "assigneeId" field this struct
// deliberately ignores; there is NO email anywhere in the result, which is
// why ${linearAllowedAssignees} must carry the display-name form (not just an
// email) for the live-resolution path to match -- see values.defaults.yaml.
// It is still decoded as json.RawMessage rather than a typed string so that
// an unexpected future shape (a nested user object) fails parseLinearAssignee
// (and therefore GetIssueDetails) closed, rather than a Go json.Unmarshal
// type-mismatch on a plain `string`-typed field silently breaking team's own
// parse too.
type linearIssueDetails struct {
	Team     string          `json:"team"`
	Assignee json.RawMessage `json:"assignee"`
}

// GetIssueDetails resolves a Linear issue/comment-parent id (e.g. "PROJ-69")
// to its team's display name AND current assignee identifier via ONE
// linear_get_issue call -- shares a single lookup between server.go's
// save_comment and save_issue-update gates, which previously issued
// separate calls for team-only resolution.
//
// team is REQUIRED: every Linear issue belongs to exactly one team, so a
// response with no resolvable team is a shape mismatch and returns an error
// (mirrors the original IssueTeam's contract, and
// PageIsUnderAnyAncestor's in ancestry.go). assignee is OPTIONAL: a real
// issue can legitimately have no assignee at all, so an absent/null
// assignee field resolves to "" with NO error -- the caller then omits the
// attr, matching Cerbos's own has()-guarded deny-assignee-outside-allowed
// rule (resource_linear.yaml), same as an ordinary update that never
// touches assignee. Only a genuinely unparseable assignee shape (present,
// but not a plain string) fails the whole call closed, since that means an
// assignee DOES exist but this code can't verify who it is -- a silent
// "no assignee" in that case would be a strictly more dangerous false
// negative than failing closed.
func GetIssueDetails(ctx context.Context, client ToolCaller, issueID string) (team, assignee string, err error) {
	result, err := client.CallTool(ctx, linearGetIssueTool, map[string]any{"id": issueID})
	if err != nil {
		return "", "", fmt.Errorf("linear issue details lookup for %q: %w", issueID, err)
	}
	var parsed linearIssueDetails
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", "", fmt.Errorf("linear issue details lookup for %q: malformed get_issue result: %w", issueID, err)
	}
	if parsed.Team == "" {
		return "", "", fmt.Errorf("linear issue details lookup for %q: get_issue result has no team", issueID)
	}
	assignee, err = parseLinearAssignee(parsed.Assignee)
	if err != nil {
		return "", "", fmt.Errorf("linear issue details lookup for %q: %w", issueID, err)
	}
	return parsed.Team, assignee, nil
}

// parseLinearAssignee decodes the raw assignee field from a linear_get_issue
// result: absent or JSON null is "no assignee" (empty string, no error); a
// JSON string is the assignee identifier directly; anything else (object,
// number, bool) is an unexpected shape this code can't verify and returns
// an error rather than silently resolving to "no assignee".
func parseLinearAssignee(raw json.RawMessage) (string, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", nil
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", fmt.Errorf("assignee field has an unexpected shape (not a plain string): %s", string(raw))
	}
	return s, nil
}

// linearGetProjectTool is the vMCP tool name for Linear's read-only project
// fetch, same recursion-safety posture as linearGetIssueTool above: keep it
// unmapped in Cerbos, or any future deny rule on it will silently fail-closed
// every save_project update-team lookup instead of the intended
// per-call fail-closed behavior tied to the actual project team check.
const linearGetProjectTool = "linear_get_project"

// linearProjectResult is the subset of linear_get_project's JSON result this
// package needs -- a project can belong to more than one team (verified live:
// e.g. "SN Support for Azure Re-platform Effort" carries just Infrastructure,
// "Database Migration Workflow and Visiblity" carries both DevOps and
// Infrastructure), so unlike linear_get_issue's single "team" string this is
// an array of {id, name, key} objects. Only "name" is used, to stay
// consistent with linearProjectAttrOption's existing addTeams/setTeams
// handling (which also compares by whatever form the caller supplied,
// resolved against ${linearAllowedTeams}'s three-identifier-form allowlist).
type linearProjectResult struct {
	Teams []struct {
		Name string `json:"name"`
	} `json:"teams"`
}

// ProjectTeams resolves a Linear project id/slug to the display names of
// every team it currently belongs to, via ONE linear_get_project call.
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error) so the caller can fail closed -- mirrors
// IssueTeam's contract above. A project with zero teams is a genuine
// Linear API invariant violation (every project requires at least one team
// on creation), so an empty result also fails closed rather than silently
// passing an empty teams list through as "nothing to check."
func ProjectTeams(ctx context.Context, client ToolCaller, projectID string) ([]string, error) {
	result, err := client.CallTool(ctx, linearGetProjectTool, map[string]any{"query": projectID})
	if err != nil {
		return nil, fmt.Errorf("linear project team lookup for %q: %w", projectID, err)
	}
	var parsed linearProjectResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return nil, fmt.Errorf("linear project team lookup for %q: malformed get_project result: %w", projectID, err)
	}
	if len(parsed.Teams) == 0 {
		return nil, fmt.Errorf("linear project team lookup for %q: get_project result has no teams", projectID)
	}
	teams := make([]string, 0, len(parsed.Teams))
	for _, t := range parsed.Teams {
		teams = append(teams, t.Name)
	}
	return teams, nil
}
