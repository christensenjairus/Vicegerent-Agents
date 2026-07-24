package server

import (
	"context"
	"testing"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path for linear_save_issue/linear_save_comment, proving the ASSIGNEE half
// of the team/assignee-resolution gate (server.go's checkLinearIssueTeam,
// upstream.GetIssueDetails): an ordinary update/comment that doesn't itself
// touch assignee gets the issue's CURRENT assignee resolved (from the SAME
// lookup the team gate already makes -- see linear_comment_team_deployed_test.go
// for the team half) and forwarded to Cerbos as the assignee attr, so
// resource_linear.yaml's pre-existing deny-assignee-outside-allowed rule
// evaluates it exactly like an explicit-assignee call. The deny *decision*
// for a non-matching assignee itself is proven by defs/linear_test.yaml.

const linearIssueResultDevopsAssignedToMe = `{"id":"PROJ-1","team":"HAHomelabs","assignee":"jchristensen@moveworks.ai"}`
const linearIssueResultDevopsAssignedToOther = `{"id":"PROJ-2","team":"HAHomelabs","assignee":"someone@example.com"}`
const linearIssueResultDevopsUnassigned = `{"id":"PROJ-3","team":"HAHomelabs"}`

func TestDeployedLinearMapping_UpdateWithNoAssigneeArgResolvesCurrentAssignee(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevopsAssignedToMe}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "PROJ-1", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one linear_get_issue lookup, got %d", up.calls)
	}
	if got, _ := d.gotAttr["teamId"].(string); got != "HAHomelabs" {
		t.Errorf("Cerbos saw teamId=%q, want the resolved team HAHomelabs", got)
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "jchristensen@moveworks.ai" {
		t.Errorf("Cerbos saw assignee=%q, want the resolved assignee jchristensen@moveworks.ai", got)
	}
}

// TestDeployedLinearMapping_UpdateWithNoAssigneeArgOnOtherAssigneeResolvesAndForwards
// proves the GATE's half of the contract: it resolves the issue's REAL
// current assignee (someone@example.com, not the operator) and hands that
// exact value to Cerbos as assignee. The actual allow/deny decision is
// Cerbos policy's job (defs/linear_test.yaml); stubDecider here always
// allows, so this only confirms what the gate SENDS.
func TestDeployedLinearMapping_UpdateWithNoAssigneeArgOnOtherAssigneeResolvesAndForwards(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevopsAssignedToOther}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "PROJ-2", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in linear_test.yaml)")
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "someone@example.com" {
		t.Errorf("Cerbos saw assignee=%q, want the resolved (non-matching) assignee someone@example.com", got)
	}
}

// TestDeployedLinearMapping_UpdateOnUnassignedIssuePassesWithNoAssigneeAttr
// proves the gate's half of the contract: it must not fail closed on a
// genuinely unassigned issue, must not carry an empty-but-present assignee
// attr on its own (Cerbos's deny-assignee-outside-allowed rule is
// has()-guarded, so a present-but-empty key would behave the same as absent
// here, but omitting it entirely matches every other has()-guarded attr in
// this shim), but DOES forward assigneeVerified=true so Cerbos's own
// deny-write-unassigned-issue rule (defs/linear_test.yaml) can make the
// actual deny decision -- stubDecider here always allows, so this only
// confirms what the gate SENDS.
func TestDeployedLinearMapping_UpdateOnUnassignedIssuePassesWithNoAssigneeAttr(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevopsUnassigned}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "PROJ-3", "title": "new title"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: unassigned issue must not fail closed, got deny: %s", res.GetError().GetReason())
	}
	if _, hasAssignee := d.gotAttr["assignee"]; hasAssignee {
		t.Errorf("expected no assignee attr for a genuinely unassigned issue, got %#v", d.gotAttr["assignee"])
	}
	if got, _ := d.gotAttr["assigneeVerified"].(bool); got != true {
		t.Errorf("expected assigneeVerified=true forwarded for a resolved-unassigned issue, got %v", d.gotAttr["assigneeVerified"])
	}
}

// TestDeployedLinearMapping_UpdateSettingAssigneeIsNotOverriddenByLookup
// proves an EXPLICIT assignee the call itself sets always wins over a live
// lookup -- the lookup still fires (to resolve the missing team), but must
// not clobber the caller's own directly-verifiable assignee value.
func TestDeployedLinearMapping_UpdateSettingAssigneeIsNotOverriddenByLookup(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevopsAssignedToOther}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "PROJ-2", "assignee": "me"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one lookup (to resolve the missing team), got %d", up.calls)
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "me" {
		t.Errorf("Cerbos saw assignee=%q, want the call's own explicit value \"me\" (must not be overridden by the lookup's someone@example.com)", got)
	}
	if _, ok := d.gotAttr["assigneeVerified"]; ok {
		t.Errorf("expected no assigneeVerified attr when the call already supplies its own assignee, got %v", d.gotAttr["assigneeVerified"])
	}
}

// TestDeployedLinearMapping_SaveCommentWithNoAssigneeSignalResolvesFromSameLookup
// proves save_comment's assignee resolution shares the SAME single lookup
// the team gate already makes (linear_comment_team_deployed_test.go covers
// the team half of this exact call).
func TestDeployedLinearMapping_SaveCommentWithNoAssigneeSignalResolvesFromSameLookup(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevopsAssignedToMe}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"issueId": "PROJ-1", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one linear_get_issue lookup (shared between team and assignee resolution), got %d", up.calls)
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "jchristensen@moveworks.ai" {
		t.Errorf("Cerbos saw assignee=%q, want the resolved assignee jchristensen@moveworks.ai", got)
	}
	if got, _ := d.gotAttr["assigneeVerified"].(bool); got != true {
		t.Errorf("expected assigneeVerified=true forwarded when the lookup resolved the assignee, got %v", d.gotAttr["assigneeVerified"])
	}
}
