package server

import (
	"context"
	"sync/atomic"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

// These tests cover the recursion hazard caused by routing the shim's own live
// lookups through the normal vmcp target. The fake below re-enters CheckRequest
// the way the real internal client does, so the reserved target and token checks
// are exercised in-process.

// reentrantUpstream feeds the shim's own lookup back into CheckRequest. depth
// records whether the lookup re-enters the authorization gate recursively.
type reentrantUpstream struct {
	s       *Server
	token   string
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
		backend = internalBackendName
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

// TestSelfLookupTool_NoRecursion verifies the reserved target short-circuits a
// valid self-token before a mapped lookup can re-enter its own live gate.
func TestSelfLookupTool_NoRecursion(t *testing.T) {
	const token = "self-tok-recursion"
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	up := &reentrantUpstream{token: token}
	// allow=true so a deny can't mask the recursion by cutting it short.
	s := New(m, e, &stubDecider{allow: true}, AuditPrincipal(),
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
			s := New(m, e, d, AuditPrincipal(),
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
			s := New(m, e, d, AuditPrincipal(),
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

func TestIsInternalBackend_MatchesDedicatedTargetOnly(t *testing.T) {
	if isInternalBackend([]string{"vmcp"}) {
		t.Fatal("normal vmcp target must not match the reserved internal target")
	}
	if !isInternalBackend([]string{internalBackendName}) {
		t.Fatal("dedicated internal MCP target must match")
	}
}
