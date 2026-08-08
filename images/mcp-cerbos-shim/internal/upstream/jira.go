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
// package needs. mcp-atlassian (sooperset/mcp-atlassian) flattens the issue
// to its own simplified dict, with assignee (or null when unassigned) at the
// TOP LEVEL, not nested under "fields", using snake_case names -- live-
// verified 2026-07-24: {"assignee":{"account_id":...,"display_name":...,
// "email":...}}. Do not assume the raw REST fields.assignee.{accountId,
// emailAddress,displayName} shape; json.Unmarshal won't error on that
// mismatch, it will silently leave Assignee nil.
//
// A genuinely unassigned issue is NOT a null/absent assignee -- mcp-atlassian
// hardcodes the sentinel object {"display_name": "Unassigned"} instead (its
// own jira/formatting.py), with account_id/email/name absent. Treat this
// sentinel as "no assignee", never as a real user's display name.
const jiraUnassignedSentinelDisplayName = "Unassigned"

type jiraIssueResult struct {
	Assignee *struct {
		AccountID   string `json:"account_id"`
		Email       string `json:"email"`
		DisplayName string `json:"display_name"`
	} `json:"assignee"`
}

// IssueAssignee resolves a Jira issue key (e.g. "PROJ-123") to its current
// assignee's identifier via ONE jira_jira_get_issue call, requesting only
// the assignee field. Returns ("", nil) for a genuinely unassigned issue
// (assignee is null/absent, OR mcp-atlassian's {"display_name":"Unassigned"}
// sentinel -- see jiraIssueResult's doc comment) -- a real Jira issue can
// have no assignee at all, mirroring linear.go's GetIssueDetails asymmetric
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
	if parsed.Assignee == nil {
		return "", nil
	}
	a := parsed.Assignee
	if a.AccountID == "" && a.Email == "" && a.DisplayName == jiraUnassignedSentinelDisplayName {
		return "", nil
	}
	switch {
	case a.Email != "":
		return a.Email, nil
	case a.DisplayName != "":
		return a.DisplayName, nil
	case a.AccountID != "":
		return a.AccountID, nil
	default:
		return "", fmt.Errorf("jira issue assignee lookup for %q: get_issue result has an assignee object with no resolvable identifier", issueKey)
	}
}
