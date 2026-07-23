package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// githubPullRequestReadTool is the vMCP tool name for GitHub's read-only PR
// fetch -- backend-prefixed the same way mapping.yaml keys its tools
// (github_pull_request_read), since that's the name this call re-enters
// CheckRequest as. Unlike notion_notion-fetch/linear_get_issue/
// pagerduty_*_get_incident, this tool stays MAPPED in Cerbos (to github_repo/
// access) rather than fully unmapped -- but every resource_github.yaml rule
// besides deny-non-allowed-repo only ever fires on attrs a bare
// pull_request_read call never populates (branch/reviewers/head/title/body
// all resolve to ” for this tool's own mapping). So the one exposure is: if
// the outer call's own repo is already outside ${githubAllowedRepos}, this
// lookup's re-entrant call gets denied too, and PRAuthor below fails closed
// with a generic error instead of the more specific repo-not-allowed message
// the outer call would otherwise have surfaced directly. The call ends up
// denied either way -- just a less specific message in that one corner case.
// See server.go's checkGithubPRAuthor doc comment.
const githubPullRequestReadTool = "github_pull_request_read"

// githubPRResult is the subset of pull_request_read (method: "get")'s JSON
// result this package needs. GitHub's REST API pull request object
// represents the PR's author as a nested {"login": ...} user reference (the
// "user" field is GitHub's own name for it, not "author") -- a well-
// documented, always-public part of the public REST API schema.
//
// NOTE: this field shape is inferred from GitHub's documented REST API
// conventions, NOT verified against a live call to this specific MCP tool
// (unlike linear.go's IssueTeam, which was confirmed against a real live
// response) -- this sandbox has no GitHub credentials to test against. If
// pull_request_read's actual result nests the author differently, PRAuthor
// fails closed (empty/malformed login resolves to an error, never a silent
// pass), so a shape mismatch denies every gated call rather than letting one
// through unchecked. Live verification against a real GitHub account is a
// mandatory follow-up before relying on this in production -- see the MR's
// own follow-up section.
type githubPRResult struct {
	User struct {
		Login string `json:"login"`
	} `json:"user"`
}

// PRAuthor resolves a GitHub pull request to its author's login via ONE
// pull_request_read (method: "get") call. Returns an error on any lookup
// failure (timeout, non-200, malformed result, tool-reported error, or a PR
// with no resolvable author login) so the caller can fail closed -- mirrors
// IssueTeam/ProjectTeams/IncidentServiceID's contract elsewhere in this
// package.
func PRAuthor(ctx context.Context, client ToolCaller, owner, repo string, pullNumber float64) (string, error) {
	result, err := client.CallTool(ctx, githubPullRequestReadTool, map[string]any{
		"owner": owner, "repo": repo, "pullNumber": pullNumber, "method": "get",
	})
	if err != nil {
		return "", fmt.Errorf("github PR author lookup for %s/%s#%v: %w", owner, repo, pullNumber, err)
	}
	var parsed githubPRResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", fmt.Errorf("github PR author lookup for %s/%s#%v: malformed pull_request_read result: %w", owner, repo, pullNumber, err)
	}
	if parsed.User.Login == "" {
		return "", fmt.Errorf("github PR author lookup for %s/%s#%v: pull_request_read result has no resolvable author login", owner, repo, pullNumber)
	}
	return parsed.User.Login, nil
}
