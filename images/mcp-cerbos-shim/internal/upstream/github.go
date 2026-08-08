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

// githubPRResult contains the author reference returned by pull_request_read
// with method "get". A missing or malformed login causes PRAuthor to return
// an error so authorization fails closed.
type githubPRResult struct {
	User struct {
		Login string `json:"login"`
	} `json:"user"`
}

// PRAuthor resolves a GitHub pull request to its author's login via ONE
// pull_request_read (method: "get") call. Returns an error on any lookup
// failure (timeout, non-200, malformed result, tool-reported error, or a PR
// with no resolvable author login) so the caller can fail closed -- mirrors
// GetIssueDetails/ProjectTeams/IncidentServiceID's contract elsewhere in this
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
