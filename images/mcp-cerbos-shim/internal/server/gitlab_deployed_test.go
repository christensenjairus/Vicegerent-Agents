package server

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("gitlab_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns a GitLab
// tool call into the gitlab_project resource Cerbos denies outside
// ${gitlabAllowedProjects}; the deny *decisions* themselves are proven by
// defs/gitlab_test.yaml.

func TestDeployedGitlabMapping_MappedToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	// One tool per category toolhive-servers.json's `tools` allowlist enables:
	// issues, labels, todos, the MR object itself, the surviving note/discussion
	// READ tools, draft-note reads, diffs, and pipelines. Every one carries a
	// project_id, so every one is scoped -- reads included (see
	// resource_gitlab.yaml on why reads are project-scoped here rather than
	// unrestricted as in jira_project). The note/thread/draft-note WRITE tools
	// are absent from the allowlist entirely (mirroring GitHub's PR-comment
	// removal), so there is nothing to map or test for them.
	cases := []struct {
		tool string
		args map[string]any
	}{
		{"gitlab_get_project", map[string]any{"project_id": "999"}},
		{"gitlab_get_issue", map[string]any{"project_id": "999", "issue_iid": "1"}},
		{"gitlab_list_labels", map[string]any{"project_id": "999"}},
		{"gitlab_list_todos", map[string]any{"project_id": "999"}},
		{"gitlab_get_merge_request", map[string]any{"project_id": "999", "merge_request_iid": "1"}},
		{"gitlab_mr_discussions", map[string]any{"project_id": "999", "merge_request_iid": "1"}},
		{"gitlab_list_draft_notes", map[string]any{"project_id": "999", "merge_request_iid": "1"}},
		{"gitlab_get_merge_request_diffs", map[string]any{"project_id": "999", "merge_request_iid": "1"}},
		{"gitlab_retry_pipeline_job", map[string]any{"project_id": "999", "job_id": 1}},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			// allow=false: the shim must forward a well-formed resource to
			// Cerbos and honor its deny (turning it into a PERMISSION_DENIED
			// error).
			d := &stubDecider{allow: false}
			s := New(m, e, d, AuditPrincipal())
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies, got pass")
			}
			assertNoSideEffects(t, res)
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "gitlab_project" {
				t.Errorf("resourceType = %q, want gitlab_project", d.gotType)
			}
			if d.gotAct != "access" {
				t.Errorf("action = %q, want access", d.gotAct)
			}
			if d.gotAttr["projectId"] != "999" {
				t.Errorf("attr = %v, want projectId=999", d.gotAttr)
			}
			if d.gotID != "999" {
				t.Errorf("resource id = %q, want 999", d.gotID)
			}
		})
	}
}

func TestDeployedGitlabMapping_AllowedProjectPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	// A path-form project_id is canonicalized to its numeric id before Cerbos
	// sees it, so this needs the canonicalizer wired (a path with no gate
	// configured fails closed -- proven by
	// TestDeployedGitlabMapping_UnconfiguredCanonicalizerFailsClosed below).
	up := &fakeUpstream{text: `{"id":"148","path_with_namespace":"hahomelabs/vicegerent-agents"}`}
	s := New(m, e, d, AuditPrincipal(),
		WithGitlabProjectCanonicalizer(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": "hahomelabs/vicegerent-agents", "issue_iid": "1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an allowed project")
	}
	if d.gotAttr["projectId"] != "148" {
		t.Errorf("attr = %v, want the canonicalized projectId=148", d.gotAttr)
	}
}

// The canonicalization gate is what lets an operator list ONE value per project
// in ${gitlabAllowedProjects} instead of guessing every spelling GitLab accepts.
// All four of these were verified against the live instance to address the same
// project (148); each must reach Cerbos as that one numeric id.
func TestDeployedGitlabMapping_EverySpellingCanonicalizesToOneID(t *testing.T) {
	for _, spelling := range []string{
		"hahomelabs/vicegerent-agents",
		"hahomelabs%2Fvicegerent-agents",
		"HAHomeLabs/Vicegerent-Agents",
		"HAHomeLabs%2FVicegerent-Agents",
	} {
		t.Run(spelling, func(t *testing.T) {
			d := &stubDecider{allow: true}
			up := &fakeUpstream{text: `{"id":"148","path_with_namespace":"hahomelabs/vicegerent-agents"}`}
			s := newGitlabServer(t, d, nil, WithGitlabProjectCanonicalizer(up))
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
					map[string]any{"project_id": spelling, "issue_iid": "1"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass; a canonicalized allowed project must not deny")
			}
			if d.gotAttr["projectId"] != "148" {
				t.Errorf("Cerbos saw projectId=%v, want 148 for spelling %q", d.gotAttr["projectId"], spelling)
			}
		})
	}
}

// A numeric project_id is already canonical, so the gate must NOT spend a
// network call on it -- this is what keeps the common path free.
func TestDeployedGitlabMapping_NumericProjectIdSkipsLookup(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: `{"id":"999"}`}
	s := newGitlabServer(t, d, nil, WithGitlabProjectCanonicalizer(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": "148", "issue_iid": "1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass")
	}
	if up.calls != 0 {
		t.Errorf("canonicalizer called %d times for an already-numeric id, want 0", up.calls)
	}
	if d.gotAttr["projectId"] != "148" {
		t.Errorf("projectId = %v, want 148 passed through untouched", d.gotAttr["projectId"])
	}
}

// targetProjectId is checked against the SAME allowlist by the same Cerbos
// rule, so it needs the same canonical form -- otherwise a cross-project link
// or MR whose target is named by path would false-deny.
func TestDeployedGitlabMapping_TargetProjectIdIsCanonicalizedToo(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: `{"id":"7","path_with_namespace":"hahomelabs/other"}`}
	s := newGitlabServer(t, d, nil, WithGitlabProjectCanonicalizer(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_create_merge_request",
			map[string]any{
				"project_id": "148", "title": "t",
				"source_branch": "feat", "target_branch": "develop",
				"target_project_id": "hahomelabs/other",
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	// create_merge_request returns Mutated, not Pass: gitlabDraftTitleForce
	// rewrites the title to "Draft: t". What this test cares about is the
	// canonicalized attr Cerbos saw, so accept either allow shape.
	if isDeny(res) {
		t.Fatalf("expected an allow (pass or mutated), got deny")
	}
	if d.gotAttr["targetProjectId"] != "7" {
		t.Errorf("targetProjectId = %v, want the canonicalized 7", d.gotAttr["targetProjectId"])
	}
	if d.gotAttr["projectId"] != "148" {
		t.Errorf("projectId = %v, want 148 (numeric, untouched)", d.gotAttr["projectId"])
	}
}

// Fail-closed contract: a lookup error denies rather than falling through with
// the un-canonical value, which would silently reinstate the exact-match
// behaviour this gate replaces.
func TestDeployedGitlabMapping_CanonicalizationLookupFailureFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream exploded")}
	s := newGitlabServer(t, d, nil, WithGitlabProjectCanonicalizer(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": "hahomelabs/vicegerent-agents", "issue_iid": "1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when the canonicalization lookup fails")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos consulted %d times; the gate must deny before Cerbos", d.calls)
	}
}

func TestDeployedGitlabMapping_UnconfiguredCanonicalizerFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	s := newGitlabServer(t, d, nil)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": "hahomelabs/vicegerent-agents", "issue_iid": "1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny for a non-numeric project with no canonicalizer configured")
	}
}

// TestDeployedGitlabMapping_NumericProjectIdIsStringified proves the reason
// gitlabProjectAttr exists at all: project_id is *declared* a string by the tool
// schema, but a caller naming a project by its numeric id sends a JSON number,
// and get()/lookupCI type-assert v.(string) -- so a plain
// `attr: {projectId: get(args,'project_id',”)}` would read 148 as absent,
// resolve to "", match no allowlist entry, and produce a confusing false deny on
// a perfectly legitimate project.
func TestDeployedGitlabMapping_NumericProjectIdIsStringified(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, AuditPrincipal())
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": 148, "issue_iid": 1})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny=%v", isDeny(res))
	}
	if d.gotAttr["projectId"] != "148" {
		t.Errorf("attr.projectId = %#v, want the stringified \"148\"", d.gotAttr["projectId"])
	}
}

// TestDeployedGitlabMapping_TargetProjectIdIsSurfaced proves the second,
// cross-project arg reaches Cerbos: create_issue_link's target_project_id (the
// link's other end) and create_merge_request's target_project_id (the MR's
// target project) would otherwise let an ALLOWED project_id carry a write whose
// effect lands in a project outside the allowlist -- the same side-channel class
// as a Jira epicKey smuggled through additional_fields.
func TestDeployedGitlabMapping_TargetProjectIdIsSurfaced(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	cases := []struct {
		tool string
		args map[string]any
	}{
		// String and numeric target_project_id both stringify to "999".
		// create_issue_link used to cover the string case, but the issue WRITE
		// tools are no longer allowlisted (read-only issue surface on both
		// forges), so create_merge_request -- the other tool carrying a second
		// project -- covers both shapes.
		{"gitlab_create_merge_request", map[string]any{
			"project_id": "148", "title": "t",
			"source_branch": "feature-x", "target_branch": "develop",
			"target_project_id": "999",
		}},
		{"gitlab_create_merge_request", map[string]any{
			"project_id": "148", "title": "t",
			"source_branch": "feature-x", "target_branch": "main",
			"target_project_id": 999,
		}},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, AuditPrincipal())
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies")
			}
			if d.gotAttr["projectId"] != "148" {
				t.Errorf("attr.projectId = %#v, want 148", d.gotAttr["projectId"])
			}
			if d.gotAttr["targetProjectId"] != "999" {
				t.Errorf("attr.targetProjectId = %#v, want the stringified \"999\"", d.gotAttr["targetProjectId"])
			}
		})
	}
}

// TestDeployedGitlabMapping_MergeRequestsOnlyForceDraftOnCreation proves the
// SHIPPED mapping's draft override applies only to create_merge_request.
//
// GitLab has no draft boolean: draft status is derived from the TITLE
// ("Draft: ..."). Verified live -- passing draft:true is silently ignored and
// the MR comes back draft:false. So the override rewrites the title, and this
// test asserts the TITLE, not a draft flag. It previously asserted draft==true
// and passed while every real agent-opened MR shipped ready-for-review.
func TestDeployedGitlabMapping_MergeRequestsOnlyForceDraftOnCreation(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool      string
		args      map[string]any
		wantTitle string // "" means: no override applies, so the call passes through
	}{
		{"gitlab_create_merge_request", map[string]any{
			"project_id": "148", "title": "t",
			"source_branch": "feature-x", "target_branch": "develop", "draft": false,
		}, "Draft: t"},
		{"gitlab_update_merge_request", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "title": "t", "draft": false,
		}, ""},
		{
			// An already-drafted title must not be double-prefixed.
			"gitlab_update_merge_request", map[string]any{
				"project_id": "148", "merge_request_iid": "42", "title": "Draft: t",
			}, "",
		},
		{
			// update_merge_request's title is OPTIONAL. An update that doesn't
			// touch the title gets no override at all -- forcing one would
			// overwrite the MR's real title with "Draft: ".
			"gitlab_update_merge_request", map[string]any{
				"project_id": "148", "merge_request_iid": "42", "state_event": "close",
			}, "",
		},
	}

	for _, tc := range cases {
		t.Run(fmt.Sprintf("%s/%s", tc.tool, tc.wantTitle), func(t *testing.T) {
			// update_merge_request also needs the MR-author gate wired (a fake
			// upstream, so its own lookup succeeds) -- this test cares about the
			// force-draft mapping, not the author gate, which has its own
			// dedicated tests in gitlab_mr_author_deployed_test.go.
			d := &stubDecider{allow: true}
			s := New(m, e, d, AuditPrincipal(),
				WithGitlabMRAuthor(&fakeUpstream{text: gitlabMRResultOwnAuthor}))
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if tc.wantTitle == "" {
				// No override applies: the call must be forwarded untouched,
				// NOT mutated with an invented title.
				if !isPass(res) {
					t.Fatalf("expected an untouched pass, got mutated=%v deny=%v", isMutated(res), isDeny(res))
				}
				return
			}
			if !isMutated(res) {
				t.Fatalf("expected a mutated (draft title) result, got pass=%v deny=%v", isPass(res), isDeny(res))
			}
			name, args := decodeMutated(t, res)
			if name != tc.tool {
				t.Errorf("mutated name = %q, want %q", name, tc.tool)
			}
			if args["title"] != tc.wantTitle {
				t.Errorf("title = %v, want %q", args["title"], tc.wantTitle)
			}
			// The override must not SET draft. Whatever the caller sent passes
			// through untouched (GitLab ignores it either way); what matters is
			// that draft was never rewritten to true, because that would be the
			// silent no-op this change replaced.
			if args["draft"] == true {
				t.Error("draft was forced to true, but GitLab ignores that field; the title prefix is the real mechanism")
			}
			if args["project_id"] != "148" {
				t.Errorf("project_id not preserved: %v", args)
			}
		})
	}
}

// TestDeployedGitlabMapping_DraftForceDoesNotBypassProjectAllowlist proves force
// only fires after Cerbos allows -- a disallowed project still denies, draft or
// not.
func TestDeployedGitlabMapping_DraftForceDoesNotBypassProjectAllowlist(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, AuditPrincipal())
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_create_merge_request",
			map[string]any{"project_id": "999", "title": "t", "source_branch": "f", "target_branch": "main"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny for a disallowed project")
	}
	if isMutated(res) {
		t.Fatalf("a denied call must never carry a mutation")
	}
}

// TestDeployedGitlabMapping_ReviewersAttrWiredOnMergeRequestCreateAndUpdate
// proves the shipped mapping's wiring: create/update_merge_request's
// reviewer_ids and assignee_ids each reach Cerbos as hasReviewers (they collapse
// into one attr because resource_gitlab.yaml's deny-reviewers rule treats them
// identically -- either pulls a human into the agent's own MR). Both are real
// JSON arrays of GitLab user ids on the wire, which a plain get() would read as
// absent. The deny decision itself is exercised in defs/gitlab_test.yaml.
func TestDeployedGitlabMapping_ReviewersAttrWiredOnMergeRequestCreateAndUpdate(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		name string
		tool string
		args map[string]any
	}{
		{"create/reviewer_ids", "gitlab_create_merge_request", map[string]any{
			"project_id": "148", "title": "t", "source_branch": "f", "target_branch": "main",
			"reviewer_ids": []any{7},
		}},
		{"create/assignee_ids", "gitlab_create_merge_request", map[string]any{
			"project_id": "148", "title": "t", "source_branch": "f", "target_branch": "main",
			"assignee_ids": []any{7},
		}},
		{"update/reviewer_ids", "gitlab_update_merge_request", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "reviewer_ids": []any{7},
		}},
		{"update/assignee_ids", "gitlab_update_merge_request", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "assignee_ids": []any{7},
		}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, AuditPrincipal(),
				WithGitlabMRAuthor(&fakeUpstream{text: gitlabMRResultOwnAuthor}))
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies")
			}
			if d.gotAttr["hasReviewers"] != "true" {
				t.Errorf("attr.hasReviewers = %q, want true -- the shipped mapping must surface a non-empty reviewer_ids/assignee_ids array", d.gotAttr["hasReviewers"])
			}
			if d.gotAttr["projectId"] != "148" {
				t.Errorf("projectId not preserved alongside hasReviewers: %v", d.gotAttr)
			}
		})
	}
}

// TestDeployedGitlabMapping_IssueWriteToolsAreUnmapped replaces an older test
// that proved create_issue/update_issue's assignee_ids was deliberately NOT
// surfaced as hasReviewers. That distinction is moot now: the issue WRITE tools
// were removed from the vMCP allowlist entirely, so GitLab's issue surface is
// read-only exactly as GitHub's is. What matters instead is that the removal
// actually took -- none of them should be a mapping key, and the issue READ
// tools must still be mapped and project-scoped.
func TestDeployedGitlabMapping_IssueWriteToolsAreUnmapped(t *testing.T) {
	m := deployedMapping(t)
	b, ok := m.Backends["vmcp"]
	if !ok {
		t.Fatal("rendered mapping has no vmcp backend")
	}
	for _, tool := range []string{
		"gitlab_create_issue", "gitlab_update_issue", "gitlab_create_issue_link",
	} {
		if _, mapped := b.Tools[tool]; mapped {
			t.Errorf("%s is still a mapping key; the issue write surface was removed "+
				"from host/mcp/toolhive-servers.json and should not be mapped here", tool)
		}
	}
	// The reads must survive the removal, still scoped to gitlab_project.
	for _, tool := range []string{
		"gitlab_get_issue", "gitlab_list_issues", "gitlab_my_issues",
		"gitlab_list_issue_discussions", "gitlab_list_issue_links", "gitlab_get_issue_link",
	} {
		if _, mapped := b.Tools[tool]; !mapped {
			t.Errorf("%s must stay mapped -- issue READS remain allowlisted on both forges", tool)
		}
	}
}

// TestDeployedGitlabMapping_RemovedToolsAreUnmapped: push_files,
// create_or_update_file and create_branch (GitLab's only branch-writing tools)
// plus merge_merge_request and approve_merge_request were removed from the tool
// allowlist entirely (toolhive-servers.json): the bot has direct SSH access to
// gitlab.hahomelabs.com, so routine git operations go through git itself, and
// nothing here may merge or approve. None should be a mapping key, confirming
// the removal actually took rather than being an accidental gap from a typo.
//
// resource_gitlab.yaml DOES carry a deny-protected-branch rule: it fires on
// merge_request target_branch (retargeting an existing MR at main/master/
// production), not on a repository branch write, since the branch-writing tools
// are gone. An earlier version of this comment claimed no such rule existed.
func TestDeployedGitlabMapping_RemovedToolsAreUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	removed := []string{
		"gitlab_push_files", "gitlab_create_or_update_file", "gitlab_create_branch",
		"gitlab_merge_merge_request", "gitlab_approve_merge_request",
	}
	for _, tool := range removed {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, AuditPrincipal())
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool,
					map[string]any{"project_id": "999", "branch": "main"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for unmapped/removed tool %q (falls through to defaultAction: allow)", tool)
			}
			if d.calls != 0 {
				t.Errorf("removed tool %q must not reach Cerbos, got %d calls", tool, d.calls)
			}
		})
	}
}

// TestDeployedGitlabMapping_TodoDoneToolsAreUnmapped documents the one residual
// gap in GitLab's coverage: mark_todo_done takes a todo id and
// mark_all_todos_done takes nothing at all, so neither carries a project_id any
// check here could scope -- the same class as Elastic's arg-less
// platform_streams_list_streams. Low-stakes (they clear entries off the bot's
// own todo list, they don't touch project content), so they stay in the tool
// allowlist and pass unmapped. This pins that as a deliberate decision rather
// than an oversight.
func TestDeployedGitlabMapping_TodoDoneToolsAreUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	for _, tool := range []string{"gitlab_mark_todo_done", "gitlab_mark_all_todos_done"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, AuditPrincipal())
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{"todo_id": "1"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for the unscopable tool %q", tool)
			}
			if d.calls != 0 {
				t.Errorf("%q must not reach Cerbos, got %d calls", tool, d.calls)
			}
		})
	}
}
