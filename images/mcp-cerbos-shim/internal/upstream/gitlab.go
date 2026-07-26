package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// gitlabGetProjectTool is the vMCP tool name for GitLab's read-only project
// fetch, used by CanonicalProjectID below to collapse every spelling of a
// project into the one numeric id the allowlist is written against.
//
// This tool IS mapped in mapping.yaml (to gitlab_project/access, like every
// other project-bearing read): an AGENT calling get_project directly must obey
// ${gitlabAllowedProjects} like any other read, and leaving it unmapped would
// be an unscoped project read.
//
// That makes the gate STRUCTURALLY recursive -- the canonicalization gate
// resolves a project by calling this tool, and this tool is gated by that same
// gate -- so the shim's own lookup must be short-circuited before it reaches the
// gate. Two independent mechanisms in server.go do that, both keyed on the
// secret self-token internal/upstream stamps on every re-entrant request:
// isInternalLookup (skips the whole gating path) and the selfLookupTools
// recursion backstop (skips it again on the tool name alone).
//
// Do not assume the routing alone prevents this. It does not: the reserved
// vmcp-internal backend was originally trusted to identify these lookups by
// name, but service_names carries the MCP target name -- `vmcp` for BOTH
// backends -- so that match never fired and this recursion reached production
// (40+ re-entries per agent call). See internalBackendName.
const gitlabGetProjectTool = "gitlab_get_project"

// gitlabProjectResult is the subset of get_project's JSON result this package
// needs. The wrapper renders numeric ids as JSON strings (verified live: a
// get_merge_request call made with a percent-encoded, mixed-case path returned
// "project_id":"148"), so `id` is decoded as a string rather than a number.
type gitlabProjectResult struct {
	ID                json.Number `json:"id"`
	PathWithNamespace string      `json:"path_with_namespace"`
}

// CanonicalProjectID resolves ANY spelling of a project GitLab accepts --
// numeric id, group/project path, percent-encoded path, or any casing of the
// latter two, all four verified live against the real instance -- to the single
// numeric id that project actually has. Without it, ${gitlabAllowedProjects}
// is matched against project_id exactly as the caller sent it, so the SAME
// project named a different (equally valid) way misses the allowlist and is
// denied: fail-closed, but a confusing false deny on legitimate work, and one
// the agent has no way to diagnose or retry out of.
//
// Returns an error on any lookup failure (timeout, non-200, malformed result,
// tool-reported error, or a project with no resolvable id) so the caller can
// fail closed -- same contract as MRAuthor above. A 404 for a project that
// genuinely doesn't exist is therefore a deny, which is correct: an
// unresolvable project is not an allowlisted one.
func CanonicalProjectID(ctx context.Context, client ToolCaller, projectID string) (string, error) {
	result, err := client.CallTool(ctx, gitlabGetProjectTool, map[string]any{"project_id": projectID})
	if err != nil {
		return "", fmt.Errorf("gitlab project canonicalization for %q: %w", projectID, err)
	}
	canonical, err := gitlabProjectIDFromText(result.Text())
	if err != nil {
		return "", fmt.Errorf("gitlab project canonicalization for %q: %w", projectID, err)
	}
	return canonical, nil
}

// gitlabProjectIDFromText pulls the numeric id out of a get_project result.
// Only `id` is accepted as the canonical value: path_with_namespace is the
// project's CURRENT path, which changes when a project is renamed or
// transferred between groups, so an allowlist written against it would silently
// stop matching after a move. The numeric id never changes.
func gitlabProjectIDFromText(body string) (string, error) {
	var p gitlabProjectResult
	if err := json.Unmarshal([]byte(body), &p); err != nil {
		return "", fmt.Errorf("get_project result is not a project object: %w", err)
	}
	if p.ID.String() == "" {
		return "", fmt.Errorf("get_project result has no resolvable project id")
	}
	return p.ID.String(), nil
}

// gitlabGetMergeRequestTool is the vMCP tool name for GitLab's read-only MR
// fetch -- backend-prefixed the same way mapping.yaml keys its tools
// (gitlab_get_merge_request), since that's the name this call re-enters
// CheckRequest as. Like github_pull_request_read, this tool stays MAPPED in
// Cerbos (to gitlab_project/access) rather than fully unmapped -- but the only
// resource_gitlab.yaml rule a bare get_merge_request call can trip is
// deny-non-allowed-project (hasReviewers/mrAuthor are never populated for it),
// and it carries the SAME project_id as the outer call being gated. So the one
// exposure is: if that project is already outside ${gitlabAllowedProjects},
// this lookup's re-entrant call is denied too and MRAuthor below fails closed
// with a generic error instead of the more specific project-not-allowed message
// the outer call would otherwise have surfaced. The call is denied either way --
// just a less specific message in that one corner case. See server.go's
// checkGitlabMRAuthor doc comment.
const gitlabGetMergeRequestTool = "gitlab_get_merge_request"

// gitlabMRResult is the subset of get_merge_request's JSON result this package
// needs. GitLab's REST API merge request object represents the MR's author as a
// nested {"username": ...} user reference, and this MCP wrapper passes it
// through directly -- verified against a live gitlab_get_merge_request call
// (author.username present for both the merge_request_iid and source_branch
// selectors; the wrapper returns numeric ids as JSON strings, which this struct
// never decodes). A shape mismatch would fail closed anyway: an
// empty/unparseable username resolves to an error, never a silent pass.
type gitlabMRResult struct {
	Author struct {
		Username string `json:"username"`
	} `json:"author"`
}

// MRAuthor resolves a GitLab merge request to its author's username via ONE
// get_merge_request call. The MR is selected the same way the gated call
// selected it -- by merge_request_iid when the caller gave one, otherwise by
// source_branch (get_merge_request accepts either) -- so the lookup can't
// resolve a DIFFERENT merge request than the one about to be written to, which
// is the entire point of the gate. Returns an error on any lookup failure
// (timeout, non-200, malformed result, tool-reported error, or an MR with no
// resolvable author username) so the caller can fail closed -- mirrors
// PRAuthor/IssueAssignee/SilenceCreatedBy's contract elsewhere in this package.
func MRAuthor(ctx context.Context, client ToolCaller, projectID, mrIID, sourceBranch string) (string, error) {
	args := map[string]any{"project_id": projectID}
	selector := "iid=" + mrIID
	if mrIID != "" {
		args["merge_request_iid"] = mrIID
	} else {
		args["source_branch"] = sourceBranch
		selector = "source_branch=" + sourceBranch
	}
	result, err := client.CallTool(ctx, gitlabGetMergeRequestTool, args)
	if err != nil {
		return "", fmt.Errorf("gitlab MR author lookup for %s %s: %w", projectID, selector, err)
	}
	username, err := gitlabMRAuthorFromText(result.Text())
	if err != nil {
		return "", fmt.Errorf("gitlab MR author lookup for %s %s: %w", projectID, selector, err)
	}
	return username, nil
}

// gitlabMRAuthorFromText pulls author.username out of a get_merge_request
// result body. A live call returns the single MR object for BOTH selectors, but
// selecting by source_branch is a list-then-pick operation on GitLab's own API,
// so the one-element-array shape is accepted too rather than fail-closed-denying
// every by-source_branch write if the wrapper stops collapsing it. An array with
// anything other than exactly one entry is an error:
// two MRs sharing a source branch means the gate cannot tell which one the
// write will land on, and guessing would defeat the check.
func gitlabMRAuthorFromText(body string) (string, error) {
	var single gitlabMRResult
	if err := json.Unmarshal([]byte(body), &single); err == nil {
		if single.Author.Username != "" {
			return single.Author.Username, nil
		}
	}
	var list []gitlabMRResult
	if err := json.Unmarshal([]byte(body), &list); err == nil {
		if len(list) != 1 {
			return "", fmt.Errorf("get_merge_request returned %d merge requests; cannot resolve a single author", len(list))
		}
		if list[0].Author.Username != "" {
			return list[0].Author.Username, nil
		}
	}
	return "", fmt.Errorf("get_merge_request result has no resolvable author username")
}
