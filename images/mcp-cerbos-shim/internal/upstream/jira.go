package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// jiraGetIssueTool is the vMCP tool name for Jira's read-only issue fetch --
// double-prefixed the same way mapping.yaml keys its tools
// (jira_jira_get_issue: {workload}_{tool} = jira_ + jira_get_issue), since
// that's the name this call re-enters CheckRequest as.
//
// RECURSION SAFETY: unlike notion_notion-fetch/linear_get_issue/
// pagerduty_*_get_incident (which must stay entirely UNMAPPED in Cerbos for
// recursion safety -- see ancestry.go's package doc), jira_jira_get_issue
// stays MAPPED -- to jira_project/action:"read" -- but is safe by
// construction: every deny rule in resource_jira.yaml is scoped to
// actions:[write] only, so a read can never be denied there. If a future
// policy change ever widens one of those rules to actions:['*'], this
// lookup would start getting denied and every assignee resolution below
// would fail closed (not silently allow -- see IssueAssignee's contract),
// but it would look like an unrelated regression; keep the Jira write rules
// scoped to actions:[write], or re-derive this lookup's safety before
// widening them.
const jiraGetIssueTool = "jira_jira_get_issue"

// jiraIssueResult is the subset of jira_jira_get_issue's JSON result this
// package needs. Jira Cloud's REST API represents an issue's assignee as a
// nested fields.assignee object carrying accountId/emailAddress/displayName
// (or a null fields.assignee when the issue is unassigned) -- this MCP
// server (sooperset/mcp-atlassian) is documented as returning "the Jira
// issue object" as JSON, so this assumes that object is a close passthrough
// of the REST shape.
//
// NOTE: this field shape is inferred from Jira Cloud's documented REST API
// conventions plus mcp-atlassian's own JSON-passthrough description, NOT
// verified against a live call to this specific MCP tool (unlike
// linear.go's team field, which was confirmed against a real live
// response) -- this sandbox has no Jira credentials to test against. Known
// footgun: Jira Cloud can suppress emailAddress from API responses entirely
// per-org privacy settings, independent of whether the ticket really is
// assigned to the configured user -- so IssueAssignee tries
// emailAddress -> displayName -> accountId in that order (matching
// ${jiraAllowedAssignees} being an email today) rather than hardcoding a
// single field. If the real shape differs further, IssueAssignee fails
// closed on an assignee object with none of the three identifiers populated
// -- only a genuinely null/absent assignee resolves to "" -- so a shape
// mismatch on an ASSIGNED issue denies the gated call rather than letting it
// through unchecked. Live verification against a real Jira account is a
// mandatory follow-up before relying on this in production -- see the MR's
// own follow-up section.
type jiraIssueResult struct {
	Fields struct {
		Assignee *struct {
			AccountID    string `json:"accountId"`
			EmailAddress string `json:"emailAddress"`
			DisplayName  string `json:"displayName"`
		} `json:"assignee"`
	} `json:"fields"`
}

// IssueAssignee resolves a Jira issue key (e.g. "PROJ-123") to its current
// assignee's identifier via ONE jira_jira_get_issue call, requesting only
// the assignee field. Returns ("", nil) for a genuinely unassigned issue
// (fields.assignee is null/absent) -- a real Jira issue can have no
// assignee at all, mirroring linear.go's GetIssueDetails asymmetric
// contract (team required, assignee optional). Returns an error on any
// lookup failure (timeout, non-200, malformed result, tool-reported error,
// or an assignee object with no resolvable identifier) so the caller can
// fail closed.
func IssueAssignee(ctx context.Context, client ToolCaller, issueKey string) (string, error) {
	result, err := client.CallTool(ctx, jiraGetIssueTool, map[string]any{"issue_key": issueKey, "fields": "assignee"})
	if err != nil {
		return "", fmt.Errorf("jira issue assignee lookup for %q: %w", issueKey, err)
	}
	var parsed jiraIssueResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", fmt.Errorf("jira issue assignee lookup for %q: malformed get_issue result: %w", issueKey, err)
	}
	if parsed.Fields.Assignee == nil {
		return "", nil
	}
	a := parsed.Fields.Assignee
	switch {
	case a.EmailAddress != "":
		return a.EmailAddress, nil
	case a.DisplayName != "":
		return a.DisplayName, nil
	case a.AccountID != "":
		return a.AccountID, nil
	default:
		return "", fmt.Errorf("jira issue assignee lookup for %q: get_issue result has an assignee object with no resolvable identifier", issueKey)
	}
}
