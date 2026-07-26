package eval

import (
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestGitlabHelpersSelfRegister(t *testing.T) {
	for _, name := range []string{"gitlabProjectAttr", "gitlabMergeRequestAttr"} {
		if _, ok := helperOptions(name); !ok {
			t.Fatalf("%s not registered; helpers_gitlab.go init() did not run", name)
		}
	}
}

func compileGitlabTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"gitlabProjectAttr", "gitlabMergeRequestAttr"},
				Tools: map[string]config.Tool{
					"gitlab_get_issue": {
						ResourceType: "gitlab_project",
						Action:       "access",
						ID:           "get(args,'project_id','')",
						AttrFrom:     "gitlabProjectAttr(args)",
					},
					"gitlab_create_merge_request": {
						ResourceType: "gitlab_project",
						Action:       "access",
						ID:           "get(args,'project_id','')",
						AttrFrom:     "gitlabMergeRequestAttr(args)",
					},
					"gitlab_update_merge_request": {
						ResourceType: "gitlab_project",
						Action:       "access",
						ID:           "get(args,'project_id','')",
						AttrFrom:     "gitlabMergeRequestAttr(args)",
					},
				},
			},
		},
	}
	e, err := Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return e
}

func evalGitlab(t *testing.T, e *Engine, tool string, args map[string]any) map[string]any {
	t.Helper()
	res, err := e.Eval(CallInput{Backend: "vmcp", Tool: tool, Args: args})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	return res.Attr
}

func TestGitlabProjectAttr_StringProjectId(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_get_issue",
		map[string]any{"project_id": "jchristensen/vicegerent-agents", "issue_iid": "1"})
	if attr["projectId"] != "jchristensen/vicegerent-agents" {
		t.Errorf("projectId = %q, want the group/project path unchanged", attr["projectId"])
	}
	if attr["targetProjectId"] != "" {
		t.Errorf("targetProjectId = %q, want empty on a tool with no such arg", attr["targetProjectId"])
	}
}

// TestGitlabProjectAttr_NumericProjectIdStringifies is the whole reason this
// helper exists instead of a plain get(args,'project_id',”): project_id is
// *declared* a string by the tool schema, but a caller naming a project by its
// numeric id sends a JSON number, and get()/lookupCI type-assert v.(string) --
// so a float64 would fall through to "" with no error, match no allowlist entry,
// and produce a confusing false deny on a legitimate project.
func TestGitlabProjectAttr_NumericProjectIdStringifies(t *testing.T) {
	e := compileGitlabTestEngine(t)
	for _, tc := range []struct {
		name string
		in   any
		want string
	}{
		{"json integer", float64(148), "148"},
		{"go int", 148, "148"},
		{"whole float has no trailing .0", 148.0, "148"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			attr := evalGitlab(t, e, "gitlab_get_issue", map[string]any{"project_id": tc.in})
			if attr["projectId"] != tc.want {
				t.Errorf("projectId = %q, want %q", attr["projectId"], tc.want)
			}
		})
	}
}

func TestGitlabProjectAttr_MissingProjectIdIsEmpty(t *testing.T) {
	// An omitted project_id (optional on list_issues/my_issues/list_todos/
	// list_merge_requests) resolves to "", matches no allowlist entry, and is
	// therefore denied -- fail-closed by construction, mirroring how
	// github_search_pull_requests with no owner/repo resolves to "/".
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_get_issue", map[string]any{})
	if attr["projectId"] != "" {
		t.Errorf("projectId = %q, want empty for an omitted arg", attr["projectId"])
	}
}

// TestGitlabProjectAttr_ArrayProjectIdIsEmpty: a non-scalar can't name one
// project, so it resolves to "" and is denied rather than being coerced into
// something that might match an allowlist entry.
func TestGitlabProjectAttr_ArrayProjectIdIsEmpty(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_get_issue",
		map[string]any{"project_id": []any{"148", "7"}})
	if attr["projectId"] != "" {
		t.Errorf("projectId = %q, want empty for a non-scalar arg", attr["projectId"])
	}
}

func TestGitlabProjectAttr_CaseInsensitiveKey(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_get_issue",
		map[string]any{"Project_ID": "148"})
	if attr["projectId"] != "148" {
		t.Errorf("projectId = %q, want 148 from a differently-cased key", attr["projectId"])
	}
}

// TestGitlabProjectAttr_TargetProjectIdSurfaced: create_issue_link and
// create_merge_request each take a SECOND project (an issue link's other end, an
// MR's target project). Without it, an allowlisted project_id could carry a write
// whose effect lands outside the allowlist -- the same side-channel class as a
// Jira epicKey smuggled through additional_fields.
func TestGitlabProjectAttr_TargetProjectIdSurfaced(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_get_issue",
		map[string]any{"project_id": "148", "target_project_id": float64(999)})
	if attr["projectId"] != "148" {
		t.Errorf("projectId = %q, want 148", attr["projectId"])
	}
	if attr["targetProjectId"] != "999" {
		t.Errorf("targetProjectId = %q, want the stringified 999", attr["targetProjectId"])
	}
}

func TestGitlabMergeRequestAttr_ReviewerAndAssigneeArraysSetTrue(t *testing.T) {
	e := compileGitlabTestEngine(t)
	for _, key := range []string{"reviewer_ids", "assignee_ids"} {
		t.Run(key, func(t *testing.T) {
			attr := evalGitlab(t, e, "gitlab_create_merge_request",
				map[string]any{"project_id": "148", key: []any{float64(7)}})
			if attr["hasReviewers"] != "true" {
				t.Errorf("hasReviewers = %q, want true for a non-empty %s", attr["hasReviewers"], key)
			}
			if attr["projectId"] != "148" {
				t.Errorf("projectId = %q, want 148 alongside hasReviewers", attr["projectId"])
			}
		})
	}
}

func TestGitlabMergeRequestAttr_EmptyArraysSetFalse(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_create_merge_request",
		map[string]any{"project_id": "148", "reviewer_ids": []any{}, "assignee_ids": []any{}})
	if attr["hasReviewers"] != "false" {
		t.Errorf("hasReviewers = %q, want false for empty arrays", attr["hasReviewers"])
	}
}

func TestGitlabMergeRequestAttr_AbsentArraysSetFalse(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_create_merge_request",
		map[string]any{"project_id": "148", "title": "t"})
	if attr["hasReviewers"] != "false" {
		t.Errorf("hasReviewers = %q, want false when neither arg is present", attr["hasReviewers"])
	}
}

// The branch attr feeds resource_gitlab.yaml's deny-protected-branch and must
// carry target_branch ONLY. source_branch names the MR's own feature branch,
// is never written to, and doubles as the get_merge_request selector the author
// gate uses -- surfacing it here would deny any MR whose feature branch happens
// to be named main, which is not the risk that rule guards.
func TestGitlabMergeRequestAttr_BranchIsTargetBranchOnly(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_update_merge_request",
		map[string]any{"project_id": "148", "source_branch": "main", "target_branch": "develop"})
	if attr["branch"] != "develop" {
		t.Errorf("branch = %q, want the target_branch develop (never source_branch)", attr["branch"])
	}
}

func TestGitlabMergeRequestAttr_ProtectedTargetBranchSurfaced(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_update_merge_request",
		map[string]any{"project_id": "148", "target_branch": "main"})
	if attr["branch"] != "main" {
		t.Errorf("branch = %q, want main so deny-protected-branch can fire", attr["branch"])
	}
}

// target_branch is optional on both tools; an omitted one must resolve to ""
// so the policy's non-empty guard leaves an ordinary MR edit alone.
func TestGitlabMergeRequestAttr_AbsentTargetBranchIsEmpty(t *testing.T) {
	attr := evalGitlab(t, compileGitlabTestEngine(t), "gitlab_update_merge_request",
		map[string]any{"project_id": "148", "title": "t"})
	if attr["branch"] != "" {
		t.Errorf("branch = %q, want empty when target_branch is absent", attr["branch"])
	}
}
