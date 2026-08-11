package eval

// GitLab-specific helpers; self-register via init().

import (
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("gitlabProjectAttr", gitlabProjectAttrOption)
	registerHelper("gitlabMergeRequestAttr", gitlabMergeRequestAttrOption)
	registerHelper("gitlabDraftTitleForce", gitlabDraftTitleForceOption)
}

// gitlabDraftTitlePrefix is how GitLab marks a merge request as a draft.
// Verified live against the instance: passing draft:true to
// create/update_merge_request is silently IGNORED (the MR comes back
// draft:false), while a title carrying this prefix sets draft:true. GitLab
// derives draft status from the title; unlike GitHub it has no draft field.
const gitlabDraftTitlePrefix = "Draft: "

// gitlabDraftTitleForceOption returns a forceFrom map that rewrites an MR's
// title to carry the draft prefix, which is the only way to force draft
// status on GitLab (see gitlabDraftTitlePrefix).
//
// This exists because `force: {draft: true}` - copied from the GitHub mapping,
// where draft IS a real boolean - was a silent no-op on GitLab, so every
// agent-opened merge request shipped ready-for-review rather than draft.
//
// Returns an EMPTY title when the override does not apply, which
// ForceOverrides drops rather than forwarding: this is needed for a title
// already prefixed (any casing, and GitLab also accepts the legacy "WIP:"
// marker), which must not be double-prefixed into "Draft: Draft: x".
func gitlabDraftTitleForceOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("gitlabDraftTitleForce",
			cel.Overload("gitlabDraftTitleForce_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					title := lookupCIScalar(m, "title")
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"title": gitlabDraftTitle(title),
					})
				}),
			),
		),
	}
}

// gitlabDraftTitle returns the draft-prefixed form of title, or "" when no
// rewrite should be applied (absent title, or one already marked draft/WIP).
func gitlabDraftTitle(title string) string {
	if strings.TrimSpace(title) == "" {
		return ""
	}
	lower := strings.ToLower(strings.TrimSpace(title))
	if strings.HasPrefix(lower, "draft:") || strings.HasPrefix(lower, "wip:") {
		return ""
	}
	return gitlabDraftTitlePrefix + title
}

// gitlabProjectAttrOption surfaces project_id (and target_project_id, where the
// tool has one) as string attrs for resource_gitlab.yaml's
// deny-non-allowed-project rule.
//
// target_project_id matters because create_issue_link and create_merge_request
// both take a SECOND project: an issue link's other end, and an MR's target
// project. Either would otherwise let an allowlisted project_id carry a write
// that lands in a project outside the allowlist - the same side-channel class as
// a Jira epicKey smuggled through additional_fields. It resolves to "" on the
// ~50 tools that have no such arg, which the policy's rule guards for.
//
// This exists instead of a plain `attr: {projectId: get(args,'project_id',”)}`
// because project_id is only *declared* a string by the tool schema while a
// caller naming a project by its numeric id routinely sends a JSON number
// instead, and lookupCI/get() type-assert v.(string) - a float64 falls through
// to the "" default with no error at all (the same silent-miss class
// githubReviewersAttr guards against for arrays). "" then matches no allowlist
// entry, so the call is denied: fail-closed, but a confusing false deny on a
// perfectly legitimate project. Stringifying the scalar here turns that into
// the correct allow/deny decision instead.
func gitlabProjectAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("gitlabProjectAttr",
			cel.Overload("gitlabProjectAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"projectId":       lookupCIScalar(m, "project_id"),
						"targetProjectId": lookupCIScalar(m, "target_project_id"),
					})
				}),
			),
		),
	}
}

// gitlabMergeRequestAttrOption is gitlabProjectAttr plus hasReviewers and
// branch, for create_merge_request/update_merge_request only.
//
// hasReviewers: these are the two tools carrying reviewer_ids/assignee_ids.
// Both are real JSON arrays on the wire (GitLab user ids), so they need the
// same array-aware presence check GitHub's reviewers arg does; a plain get()
// would silently read them as absent. Both collapse into one hasReviewers attr
// because resource_gitlab.yaml's deny-reviewers rule treats them identically -
// either one pulls a human into the agent's MR. Deliberately NOT applied to
// create_issue/update_issue, which carry assignee_ids too: issue assignment
// stays unrestricted (see that policy's deny-reviewers comment).
//
// branch: target_branch ONLY, feeding resource_gitlab.yaml's
// deny-protected-branch - the GitLab analog of the branch attr github_repo's
// own deny-protected-branch reads. source_branch is deliberately excluded: it
// names the MR's own feature branch (and doubles as the get_merge_request
// selector the author gate uses), nothing is written to it, and an MR whose
// source is named main is not the risk this guards. Retargeting an existing MR
// at main via update_merge_request IS that risk, and is what this catches.
func gitlabMergeRequestAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("gitlabMergeRequestAttr",
			cel.Overload("gitlabMergeRequestAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					hasReviewers := anyMapHasNonEmptyArrayOrString(m, "reviewer_ids") ||
						anyMapHasNonEmptyArrayOrString(m, "assignee_ids")
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"projectId":       lookupCIScalar(m, "project_id"),
						"targetProjectId": lookupCIScalar(m, "target_project_id"),
						"hasReviewers":    strconv.FormatBool(hasReviewers),
						"branch":          lookupCIScalar(m, "target_branch"),
					})
				}),
			),
		),
	}
}

// lookupCIScalar reads key case-insensitively and renders a string, number, or
// bool as its string form; anything else (array, object, missing key) yields
// "". Numbers render without a trailing ".0" so a JSON 148 and a JSON "148"
// both resolve to the same "148" an allowlist entry is written as.
func lookupCIScalar(m map[string]any, key string) string {
	v, ok := caseInsensitiveGet(m, key)
	if !ok {
		return ""
	}
	switch t := v.(type) {
	case string:
		return t
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case int64:
		return strconv.FormatInt(t, 10)
	case int:
		return strconv.Itoa(t)
	case bool:
		return strconv.FormatBool(t)
	}
	return ""
}
