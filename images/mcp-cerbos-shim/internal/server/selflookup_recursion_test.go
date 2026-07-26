package server

import (
	"context"
	"sync/atomic"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

// These tests cover the recursion hazard that reached production in v0.33.8 and
// that NO existing test could observe: every other canonicalization test injects
// a fake upstream that returns a canned answer WITHOUT re-entering the shim, so
// the gate calling itself is invisible to them. The fake here re-enters
// CheckRequest exactly as the real re-entrant lookup does, which is what makes
// the recursion reproducible in-process.
//
// Production shape being reproduced: the GitLab canonicalization gate resolves a
// non-numeric project_id by calling gitlab_get_project, and gitlab_get_project is
// itself mapped to gitlab_project/access. The reserved vmcp-internal backend was
// trusted to keep the shim's own lookup off the gated path, but service_names
// carries the MCP *target* name -- `vmcp` for BOTH backends in vmcp.yaml -- so
// isInternalBackend never matched, the lookup was gated, and each gated lookup
// issued another. One agent call produced 40+ identical denies.

// reentrantUpstream is a ToolCaller that feeds the shim's own lookup back into
// CheckRequest, carrying the self-token the way internal/upstream does. depth
// records the deepest nesting reached and calls the total re-entries, so a test
// can assert the gate does not invoke itself rather than merely that it returned
// something.
type reentrantUpstream struct {
	s     *Server
	token string
	// backend is the service_names value the re-entrant call arrives with.
	// Defaults to "vmcp" -- the deployed reality (both backends declare target
	// `vmcp`), NOT "vmcp-internal".
	backend string

	calls atomic.Int32
	depth atomic.Int32
	maxD  atomic.Int32
}

func (r *reentrantUpstream) CallTool(ctx context.Context, tool string, args map[string]any) (*upstream.CallToolResult, error) {
	r.calls.Add(1)
	d := r.depth.Add(1)
	defer r.depth.Add(-1)
	for {
		old := r.maxD.Load()
		if d <= old || r.maxD.CompareAndSwap(old, d) {
			break
		}
	}

	// Runaway guard: without the fix this recurses until the stack dies, which
	// would crash the whole test binary instead of failing one test. Bail out
	// well before that and let the assertions report the real depth.
	if d > 8 {
		return &upstream.CallToolResult{Content: []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}{{Type: "text", Text: `{"id":"148"}`}}}, nil
	}

	backend := r.backend
	if backend == "" {
		backend = "vmcp"
	}
	req := mcpReq(backend, "tools/call", toolCall(tool, args))
	req.Headers = []*pb.McpHeader{{Key: upstream.SelfHeaderName, Value: []byte(r.token)}}

	// The result of the re-entrant gate check is deliberately ignored: what
	// matters is whether reaching it triggers ANOTHER lookup (recursion).
	if _, err := r.s.CheckRequest(ctx, req); err != nil {
		return nil, err
	}
	return &upstream.CallToolResult{Content: []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}{{Type: "text", Text: `{"id":"148","path_with_namespace":"jchristensen/vicegerent-agents"}`}}}, nil
}

// TestSelfLookupTool_NoRecursion is the regression test for the production
// storm. A path-form project_id forces the canonicalization lookup; that lookup
// re-enters CheckRequest on the SAME backend name agent traffic uses, presenting
// the self-token. Exactly one lookup must occur: the re-entrant
// gitlab_get_project has to short-circuit on the selfLookupTools backstop rather
// than reach the canonicalization gate again.
//
// Mutation-verified: removing the selfLookupTools guard from CheckRequest fails
// this with "lookup depth = 9, want 1".
func TestSelfLookupTool_NoRecursion(t *testing.T) {
	const token = "self-tok-recursion"
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	up := &reentrantUpstream{token: token}
	// allow=true so a deny can't mask the recursion by cutting it short.
	s := New(m, e, &stubDecider{allow: true}, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithSelfToken(token), WithGitlabProjectCanonicalizer(up))
	up.s = s

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_get_issue",
			map[string]any{"project_id": "jchristensen/vicegerent-agents", "issue_iid": "1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected the agent call to be allowed, got deny (recursion likely failed the gate closed)")
	}
	if got := up.maxD.Load(); got != 1 {
		t.Errorf("lookup depth = %d, want 1 (the gate re-entered itself)", got)
	}
	if got := up.calls.Load(); got != 1 {
		t.Errorf("canonicalization lookups = %d, want 1", got)
	}
}

// TestSelfLookupTool_AgentCallStillGated is the security half: the backstop must
// only ever exempt the SHIM. An agent calling gitlab_get_project directly has no
// self-token, so it must still reach Cerbos and be denied outside the allowlist.
// Without this, the recursion fix would silently become an authz bypass on a
// project-bearing read.
func TestSelfLookupTool_AgentCallStillGated(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	for _, tool := range []string{
		"gitlab_get_project",
		"gitlab_get_merge_request",
		"github_pull_request_read",
		"jira_jira_get_issue",
	} {
		t.Run(tool+"/no token", func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
				WithSelfToken("self-tok"))
			// Numeric/simple ids so no canonicalization lookup is needed --
			// this isolates the authz question from the recursion one.
			args := map[string]any{"project_id": "999", "merge_request_iid": "1",
				"owner": "o", "repo": "r", "pullNumber": 1, "issue_key": "PROJ-1"}
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("a tokenless agent call to %s must stay gated, got pass", tool)
			}
			if d.calls != 1 {
				t.Errorf("expected exactly one Cerbos check for %s, got %d", tool, d.calls)
			}
		})

		t.Run(tool+"/wrong token", func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
				WithSelfToken("self-tok"))
			req := mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{
				"project_id": "999", "merge_request_iid": "1",
				"owner": "o", "repo": "r", "pullNumber": 1, "issue_key": "PROJ-1"}))
			req.Headers = []*pb.McpHeader{{Key: upstream.SelfHeaderName, Value: []byte("not-the-token")}}
			res, err := s.CheckRequest(context.Background(), req)
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("a forged self-token on %s must not bypass the gate, got pass", tool)
			}
			if d.calls != 1 {
				t.Errorf("expected exactly one Cerbos check for %s, got %d", tool, d.calls)
			}
		})
	}
}

// TestIsInternalBackend_DoesNotMatchDeployedTargetName pins the root cause so a
// future reader cannot re-derive the original wrong assumption. ext_mcp.proto
// documents service_names as backend names "in their native (unmuxed)
// namespace" -- the MCP target name from spec.mcp.targets[].name, not the
// AgentgatewayBackend's metadata.name -- and both vmcp.yaml backends declare
// target `vmcp`. So a real re-entrant lookup does NOT satisfy
// isInternalBackend, which is precisely why the tool-name backstop exists.
func TestIsInternalBackend_DoesNotMatchDeployedTargetName(t *testing.T) {
	if isInternalBackend([]string{"vmcp"}) {
		t.Fatal("isInternalBackend matched the deployed target name; the vmcp.yaml topology changed -- re-check whether the selfLookupTools backstop is still the load-bearing guard")
	}
	if !isInternalBackend([]string{internalBackendName}) {
		t.Error("isInternalBackend must still match the literal backend name")
	}
}
