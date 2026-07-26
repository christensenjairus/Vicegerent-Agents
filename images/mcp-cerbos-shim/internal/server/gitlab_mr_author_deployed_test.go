package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path
// for the one GitLab tool that writes to an EXISTING merge request's own fields
// (update_merge_request), with a FAKE upstream (no network) standing in for the
// live vMCP get_merge_request call the author-resolution gate makes. They prove
// the gate wiring: the MR's real author is resolved and forwarded to Cerbos as
// the mrAuthor attr, the MR is selected the same way the gated call selected it
// (iid or source_branch), a lookup failure fails closed, an unconfigured gate
// fails closed, and create_merge_request/the read tools/the comment tools are
// untouched by this gate entirely. The deny-not-own-mr *decision* itself
// (author != ${gitlabUsername}) is proven separately by defs/gitlab_test.yaml.

// gitlabMRResult* mirror get_merge_request's inferred REST-API-convention result
// shape (see upstream/gitlab.go's own caveat: NOT verified against a live call).
const gitlabMRResultOwnAuthor = `{"iid":42,"title":"some MR","author":{"username":"jchristensen"}}`
const gitlabMRResultOtherAuthor = `{"iid":99,"title":"some MR","author":{"username":"someoneelse"}}`
const gitlabMRResultNoAuthor = `{"iid":7,"title":"some MR"}`

// gitlabMRResultList is the by-source_branch shape: selecting an MR by its
// source branch is a list-then-pick operation on GitLab's own API, so the
// wrapper may hand back a one-element array instead of a bare object.
const gitlabMRResultList = `[{"iid":42,"title":"some MR","author":{"username":"jchristensen"}}]`

// gitlabMRResultTwoMatches is two MRs sharing a source branch -- the gate cannot
// tell which one the write lands on, so it must fail closed rather than guess.
const gitlabMRResultTwoMatches = `[{"iid":42,"author":{"username":"jchristensen"}},{"iid":43,"author":{"username":"jchristensen"}}]`

// newGitlabServer builds a Server over the SHIPPED mapping. up, when non-nil,
// wires the MR-author gate; extra options (e.g. the project canonicalizer) are
// appended so a test can enable exactly the gates it exercises.
func newGitlabServer(t *testing.T, d *stubDecider, up upstream.ToolCaller, extra ...Option) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	opts := extra
	if up != nil {
		opts = append(opts, WithGitlabMRAuthor(up))
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, opts...)
}

func TestDeployedGitlabMapping_UpdateMergeRequestResolvesAndForwardsAuthor(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: gitlabMRResultOwnAuthor}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "merge_request_iid": "42", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	// update_merge_request also carries the draft override, so an allowed call
	// comes back Mutated rather than a bare Pass.
	if !isMutated(res) {
		t.Fatalf("expected a mutated (draft title rewrite) result, got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one get_merge_request lookup, got %d", up.calls)
	}
	if up.gotTool != "gitlab_get_merge_request" {
		t.Errorf("lookup used tool %q, want gitlab_get_merge_request", up.gotTool)
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if d.gotType != "gitlab_project" || d.gotAct != "access" {
		t.Errorf("Cerbos saw resource=%q action=%q, want gitlab_project/access", d.gotType, d.gotAct)
	}
	if got, _ := d.gotAttr["mrAuthor"].(string); got != "jchristensen" {
		t.Errorf("Cerbos saw mrAuthor=%q, want the resolved author jchristensen", got)
	}
	_, args := decodeMutated(t, res)
	// GitLab has no draft boolean — draft comes from the title prefix, so the
	// override rewrites the title rather than setting draft:true (which the
	// API silently ignores). Asserting the title is what makes this test
	// reflect deployed behaviour: it previously asserted draft==true, which
	// passed while every real agent-opened MR shipped non-draft.
	if args["title"] != "Draft: new title" {
		t.Errorf("forced arg title = %v, want %q", args["title"], "Draft: new title")
	}
	if _, present := args["draft"]; present {
		t.Errorf("draft arg should not be forced on GitLab (it is ignored by the API), got %v", args["draft"])
	}
}

// TestDeployedGitlabMapping_UpdateOnOtherAuthorResolvesAndForwards proves the
// GATE's half of the contract: it resolves the MR's real author (someoneelse,
// not the operator) and hands that exact value to Cerbos as mrAuthor. The
// allow/deny decision for a non-matching mrAuthor is Cerbos policy's job,
// covered by defs/gitlab_test.yaml's deny-not-own-mr case -- this test uses
// stubDecider (a fixed verdict, not real policy logic) only to confirm what the
// gate SENDS, not what Cerbos DECIDES.
func TestDeployedGitlabMapping_UpdateOnOtherAuthorResolvesAndForwards(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: gitlabMRResultOtherAuthor}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "merge_request_iid": "99", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected no deny: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in gitlab_test.yaml)")
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if got, _ := d.gotAttr["mrAuthor"].(string); got != "someoneelse" {
		t.Errorf("Cerbos saw mrAuthor=%q, want the resolved (non-matching) author someoneelse", got)
	}
}

// TestDeployedGitlabMapping_UpdateBySourceBranchResolvesTheSameMR proves the
// gate follows update_merge_request's OTHER selector. The tool accepts either
// merge_request_iid or source_branch, so a gate that only understood the iid
// would inject no mrAuthor at all for a by-source_branch write and let it skip
// deny-not-own-mr entirely -- the lookup passes source_branch straight through
// so it can't resolve a DIFFERENT merge request than the one being written to.
func TestDeployedGitlabMapping_UpdateBySourceBranchResolvesTheSameMR(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: gitlabMRResultList}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "source_branch": "feature-x", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected no deny for an own-authored MR selected by source_branch")
	}
	if up.calls != 1 {
		t.Fatalf("expected exactly one get_merge_request lookup, got %d", up.calls)
	}
	if got, _ := up.gotArgs["source_branch"].(string); got != "feature-x" {
		t.Errorf("lookup selected by source_branch=%q, want feature-x (it must resolve the same MR the write targets)", got)
	}
	if _, hasIID := up.gotArgs["merge_request_iid"]; hasIID {
		t.Errorf("lookup must not invent a merge_request_iid the caller never sent: %v", up.gotArgs)
	}
	if got, _ := d.gotAttr["mrAuthor"].(string); got != "jchristensen" {
		t.Errorf("Cerbos saw mrAuthor=%q, want jchristensen resolved from the one-element list shape", got)
	}
}

// TestDeployedGitlabMapping_AmbiguousSourceBranchFailsClosed: two MRs sharing a
// source branch means the gate cannot tell which one the write will land on.
// Guessing would defeat the check, so it denies.
func TestDeployedGitlabMapping_AmbiguousSourceBranchFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: gitlabMRResultTwoMatches}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "source_branch": "feature-x", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when source_branch matches more than one MR")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedGitlabMapping_MRAuthorLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "merge_request_iid": "42", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the author lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

// TestDeployedGitlabMapping_NoResolvableMRAuthorFailsClosed is the shape-mismatch
// safety net: upstream/gitlab.go's author.username field shape is inferred from
// GitLab's documented REST conventions, not verified against a live call to this
// MCP tool. If it's wrong, every gated call denies rather than one slipping
// through unchecked.
func TestDeployedGitlabMapping_NoResolvableMRAuthorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: gitlabMRResultNoAuthor}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "merge_request_iid": "7", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed): get_merge_request result had no resolvable author")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedGitlabMapping_UnconfiguredMRAuthorGateFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	// No WithGitlabMRAuthor: production's main.go always wires it, so reaching
	// here unconfigured means a broken deploy, not a license to allow an
	// unscoped MR write through -- same posture as the GitHub PR-author gate's
	// own unconfigured-gate test.
	s := newGitlabServer(t, d, nil)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_update_merge_request",
			map[string]any{"project_id": "148", "merge_request_iid": "42", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny with the author gate unconfigured (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached with the gate unconfigured, got %d calls", d.calls)
	}
}

// TestDeployedGitlabMapping_CreateMergeRequestDoesNotTriggerAuthorGate: there is
// no prior MR to look up, and the bot's own token authors whatever it creates.
// Its force:{draft:true} mapping still applies independently.
func TestDeployedGitlabMapping_CreateMergeRequestDoesNotTriggerAuthorGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newGitlabServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_create_merge_request",
			map[string]any{"project_id": "148", "title": "t", "source_branch": "feature-x", "target_branch": "main"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isMutated(res) {
		t.Fatalf("expected a mutated (force draft:true) result, got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if up.calls != 0 {
		t.Errorf("create_merge_request must not trigger the MR-author lookup gate, got %d calls", up.calls)
	}
	if _, hasAuthor := d.gotAttr["mrAuthor"]; hasAuthor {
		t.Errorf("create_merge_request must carry no mrAuthor attr, got %#v", d.gotAttr["mrAuthor"])
	}
}

// TestDeployedGitlabMapping_CommentAndReadToolsDoNotTriggerAuthorGate proves the
// deliberate scope of this gate. Every one of these shares the same
// gitlab_project/access resource/action pair as update_merge_request, but none is
// gated: the note/thread/discussion/draft-note tools comment on an MR without
// mutating its own fields, and reviewing merge requests the agent does NOT own is
// exactly why they stay in the tool allowlist (unlike GitHub, where the operator
// removed every PR-comment tool outright).
func TestDeployedGitlabMapping_CommentAndReadToolsDoNotTriggerAuthorGate(t *testing.T) {
	cases := []struct {
		tool string
		args map[string]any
	}{
		{"gitlab_get_merge_request", map[string]any{"project_id": "148", "merge_request_iid": "42"}},
		{"gitlab_mr_discussions", map[string]any{"project_id": "148", "merge_request_iid": "42"}},
		{"gitlab_create_note", map[string]any{
			"project_id": "148", "noteable_type": "merge_request",
			"noteable_iid": 42, "body": "looks good",
		}},
		{"gitlab_create_merge_request_thread", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "body": "a review comment",
		}},
		{"gitlab_create_merge_request_note", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "discussion_id": "d1", "body": "reply",
		}},
		{"gitlab_resolve_merge_request_thread", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "discussion_id": "d1", "resolved": true,
		}},
		{"gitlab_create_draft_note", map[string]any{
			"project_id": "148", "merge_request_iid": "42", "note": "a draft",
		}},
	}
	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			d := &stubDecider{allow: true}
			up := &fakeUpstream{err: errors.New("must not be called")}
			s := newGitlabServer(t, d, up)
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass, got deny=%v mutated=%v", isDeny(res), isMutated(res))
			}
			if up.calls != 0 {
				t.Errorf("%s must not trigger the MR-author lookup gate, got %d calls", tc.tool, up.calls)
			}
			if _, hasAuthor := d.gotAttr["mrAuthor"]; hasAuthor {
				t.Errorf("%s must carry no mrAuthor attr, got %#v", tc.tool, d.gotAttr["mrAuthor"])
			}
		})
	}
}
