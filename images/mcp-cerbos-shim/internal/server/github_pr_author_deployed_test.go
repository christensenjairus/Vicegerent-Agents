package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path for the three GitHub tools that write to an EXISTING pull request
// (update_pull_request, update_pull_request_branch, request_copilot_review),
// with a FAKE upstream (no network) standing in for the live vMCP
// pull_request_read call the author-resolution gate makes. They prove the
// gate wiring: the PR's real author is resolved and forwarded to Cerbos as
// the prAuthor attr, a lookup failure fails closed, an unconfigured gate
// fails closed, and create_pull_request/the read tools are untouched by this
// gate entirely. The deny-not-own-pr *decision* itself (author != allowed
// user) is proven separately by defs/github_test.yaml.

// githubPRResultOwnAuthor/githubPRResultOtherAuthor mirror pull_request_read's
// inferred REST-API-convention result shape (see upstream/github.go's own
// caveat: NOT verified against a live call).
const githubPRResultOwnAuthor = `{"number":42,"title":"some PR","user":{"login":"christensenjairus"}}`
const githubPRResultOtherAuthor = `{"number":99,"title":"some PR","user":{"login":"someoneelse"}}`
const githubPRResultNoAuthor = `{"number":7,"title":"some PR"}`

func newGithubServer(t *testing.T, d *stubDecider, up upstream.ToolCaller) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	if up == nil {
		return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithGithubPRAuthor(up))
}

func TestDeployedGithubMapping_UpdatePullRequestBranchResolvesAndForwardsAuthor(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: githubPRResultOwnAuthor}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_update_pull_request_branch",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "pullNumber": 42})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: PR is authored by the allowed user")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one pull_request_read lookup, got %d", up.calls)
	}
	if up.gotTool != "github_pull_request_read" {
		t.Errorf("lookup used tool %q, want github_pull_request_read", up.gotTool)
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if d.gotType != "github_repo" || d.gotAct != "access" {
		t.Errorf("Cerbos saw resource=%q action=%q, want github_repo/access", d.gotType, d.gotAct)
	}
	if got, _ := d.gotAttr["prAuthor"].(string); got != "christensenjairus" {
		t.Errorf("Cerbos saw prAuthor=%q, want the resolved author christensenjairus", got)
	}
}

// TestDeployedGithubMapping_RequestCopilotReviewOnOtherAuthorResolvesAndForwards
// proves the GATE's half of the contract: it resolves the PR's real author
// (someoneelse, not the operator) and hands that exact value to Cerbos as
// prAuthor. The actual allow/deny decision for a non-matching prAuthor is
// Cerbos policy's job, already covered by defs/github_test.yaml's
// deny-not-own-pr case -- this test uses stubDecider (a fixed verdict, not
// real policy logic) only to confirm what the gate SENDS, not what Cerbos
// DECIDES.
func TestDeployedGithubMapping_RequestCopilotReviewOnOtherAuthorResolvesAndForwards(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: githubPRResultOtherAuthor}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_request_copilot_review",
			map[string]any{"owner": "someoneelse", "repo": "some-repo", "pullNumber": 99})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in github_test.yaml)")
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if got, _ := d.gotAttr["prAuthor"].(string); got != "someoneelse" {
		t.Errorf("Cerbos saw prAuthor=%q, want the resolved (non-matching) author someoneelse", got)
	}
}

// TestDeployedGithubMapping_UpdatePullRequestOnOwnPRStillForcesDraft proves the
// author gate composes correctly with the PRE-EXISTING force:{draft:true}
// mapping on update_pull_request: an allowed call (own PR) must still come
// back Mutated with draft forced, not a bare Pass.
func TestDeployedGithubMapping_UpdatePullRequestOnOwnPRStillForcesDraft(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: githubPRResultOwnAuthor}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_update_pull_request",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "pullNumber": 42, "draft": false})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isMutated(res) {
		t.Fatalf("expected a mutated (force draft:true) result, got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if got, _ := d.gotAttr["prAuthor"].(string); got != "christensenjairus" {
		t.Errorf("Cerbos saw prAuthor=%q, want christensenjairus", got)
	}
	_, args := decodeMutated(t, res)
	if args["draft"] != true {
		t.Errorf("forced arg draft = %v, want true", args["draft"])
	}
}

func TestDeployedGithubMapping_AuthorLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_update_pull_request_branch",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "pullNumber": 42})))
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

func TestDeployedGithubMapping_NoResolvableAuthorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: githubPRResultNoAuthor}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_update_pull_request_branch",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "pullNumber": 7})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed): pull_request_read result had no resolvable author")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedGithubMapping_UnconfiguredAuthorGateFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	// No WithGithubPRAuthor: production's main.go always wires it, so
	// reaching here unconfigured means a broken deploy, not a license to
	// allow an unscoped PR write through -- same posture as the Notion
	// ancestry gate's/PagerDuty service gate's unconfigured-gate tests.
	s := newGithubServer(t, d, nil)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_update_pull_request_branch",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "pullNumber": 42})))
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

// TestDeployedGithubMapping_CreatePullRequestDoesNotTriggerAuthorGate proves
// create_pull_request (a DIFFERENT tool sharing the same github_repo/access
// resource/action pair) is untouched by this gate -- there is no prior PR to
// look up, and the bot's own token authors whatever it creates. Its
// pre-existing force:{draft:true} mapping still applies independently.
func TestDeployedGithubMapping_CreatePullRequestDoesNotTriggerAuthorGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newGithubServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_create_pull_request",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "base": "main", "head": "feature-x", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isMutated(res) {
		t.Fatalf("expected a mutated (force draft:true) result, got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if up.calls != 0 {
		t.Errorf("create_pull_request must not trigger the PR-author lookup gate, got %d calls", up.calls)
	}
	if _, hasAuthor := d.gotAttr["prAuthor"]; hasAuthor {
		t.Errorf("create_pull_request must carry no prAuthor attr, got %#v", d.gotAttr["prAuthor"])
	}
}

// TestDeployedGithubMapping_ReadToolsDoNotTriggerAuthorGate proves the
// read-only tools sharing the same github_repo/access resource/action pair
// (pull_request_read itself, list/search_pull_requests) are untouched.
func TestDeployedGithubMapping_ReadToolsDoNotTriggerAuthorGate(t *testing.T) {
	for _, tool := range []string{"github_pull_request_read", "github_list_pull_requests", "github_search_pull_requests"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: true}
			up := &fakeUpstream{err: errors.New("must not be called")}
			s := newGithubServer(t, d, up)
			args := map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents"}
			if tool == "github_pull_request_read" {
				args["pullNumber"] = 42
				args["method"] = "get"
			} else {
				args["query"] = "is:pr"
			}
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass, got deny=%v mutated=%v", isDeny(res), isMutated(res))
			}
			if up.calls != 0 {
				t.Errorf("%s must not trigger the PR-author lookup gate, got %d calls", tool, up.calls)
			}
		})
	}
}
