package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path for the three Jira tools that edit an EXISTING issue's own content
// (update_issue, add_comment, transition_issue), with a FAKE upstream (no
// network) standing in for the live vMCP jira_jira_get_issue call the
// assignee-resolution gate makes. They prove the gate wiring: the issue's
// CURRENT assignee is resolved and forwarded to Cerbos as the assignee
// attr, a lookup failure fails closed, an unconfigured gate fails closed,
// and an unassigned issue passes with no assignee attr at all. The deny
// decision for a non-matching assignee itself (deny-assignee-outside-allowed)
// is proven separately by defs/jira_test.yaml.

const jiraIssueResultAssignedToMe = `{"fields":{"assignee":{"emailAddress":"jchristensen@moveworks.ai"}}}`
const jiraIssueResultAssignedToOther = `{"fields":{"assignee":{"emailAddress":"someone@example.com"}}}`
const jiraIssueResultUnassigned = `{"fields":{"assignee":null}}`

func newJiraServer(t *testing.T, d *stubDecider, up upstream.ToolCaller) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	if up == nil {
		return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithJiraIssueAssignee(up))
}

func TestDeployedJiraMapping_UpdateIssueWithNoAssigneeArgResolvesCurrentAssignee(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: jiraIssueResultAssignedToMe}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_update_issue",
			map[string]any{"issue_key": "CHANGE-1", "fields": `{"summary": "new summary"}`})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one jira_jira_get_issue lookup, got %d", up.calls)
	}
	if up.gotTool != "jira_jira_get_issue" {
		t.Errorf("lookup used tool %q, want jira_jira_get_issue", up.gotTool)
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "jchristensen@moveworks.ai" {
		t.Errorf("Cerbos saw assignee=%q, want the resolved assignee jchristensen@moveworks.ai", got)
	}
}

// TestDeployedJiraMapping_TransitionIssueOnOtherAssigneeResolvesAndForwards
// proves the GATE's half of the contract: it resolves the issue's REAL
// current assignee (someone else, not the operator) and hands that exact
// value to Cerbos as assignee. The actual allow/deny decision for a
// non-matching assignee is Cerbos policy's job, already covered by
// defs/jira_test.yaml's assignee-scoping case -- this test uses stubDecider
// (a fixed verdict, not real policy logic) only to confirm what the gate
// SENDS, not what Cerbos DECIDES.
func TestDeployedJiraMapping_TransitionIssueOnOtherAssigneeResolvesAndForwards(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: jiraIssueResultAssignedToOther}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_transition_issue",
			map[string]any{"issue_key": "CHANGE-2", "transition_id": "31"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in jira_test.yaml)")
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "someone@example.com" {
		t.Errorf("Cerbos saw assignee=%q, want the resolved (non-matching) assignee someone@example.com", got)
	}
}

// TestDeployedJiraMapping_AddCommentOnUnassignedIssuePassesWithNoAssigneeAttr
// proves the asymmetric contract: an issue with genuinely NO assignee must
// not fail closed, and must not carry an empty-but-present assignee attr
// either (Cerbos's deny-assignee-outside-allowed rule is has()-guarded).
func TestDeployedJiraMapping_AddCommentOnUnassignedIssuePassesWithNoAssigneeAttr(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: jiraIssueResultUnassigned}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_add_comment",
			map[string]any{"issue_key": "CHANGE-3", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: unassigned issue must not fail closed, got deny: %s", res.GetError().GetReason())
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "" {
		t.Errorf("expected empty assignee attr for a genuinely unassigned issue, got %q", got)
	}
}

// TestDeployedJiraMapping_UpdateIssueSettingAssigneeIsNotOverriddenByLookup
// proves an EXPLICIT assignee smuggled into update_issue's fields JSON
// always wins over a live lookup -- the lookup must not even fire, since
// jiraFieldsAttr already resolved a verifiable, non-empty assignee directly.
func TestDeployedJiraMapping_UpdateIssueSettingAssigneeIsNotOverriddenByLookup(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_update_issue",
			map[string]any{"issue_key": "CHANGE-4", "fields": `{"assignee": "jchristensen@moveworks.ai"}`})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 0 {
		t.Errorf("expected no lookup when the call already sets a verifiable assignee, got %d calls", up.calls)
	}
	if got, _ := d.gotAttr["assignee"].(string); got != "jchristensen@moveworks.ai" {
		t.Errorf("Cerbos saw assignee=%q, want the call's own explicit value", got)
	}
}

func TestDeployedJiraMapping_AssigneeLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_update_issue",
			map[string]any{"issue_key": "CHANGE-5", "fields": `{"summary": "x"}`})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the assignee lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedJiraMapping_UnconfiguredAssigneeGateFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	// No WithJiraIssueAssignee: production's main.go always wires it, so
	// reaching here unconfigured means a broken deploy, not a license to
	// allow an unscoped ticket write through.
	s := newJiraServer(t, d, nil)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_transition_issue",
			map[string]any{"issue_key": "CHANGE-6", "transition_id": "31"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny with the assignee gate unconfigured (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached with the gate unconfigured, got %d calls", d.calls)
	}
}

// TestDeployedJiraMapping_CreateIssueLinkDoesNotTriggerAssigneeGate proves
// create_issue_link/link_to_epic (which relate two already-project-scoped
// tickets rather than edit either ticket's own content) are untouched by
// this gate.
func TestDeployedJiraMapping_CreateIssueLinkDoesNotTriggerAssigneeGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newJiraServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_create_issue_link",
			map[string]any{"inward_issue_key": "CHANGE-1", "outward_issue_key": "CHANGE-2", "link_type": "Blocks"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.calls != 0 {
		t.Errorf("create_issue_link must not trigger the assignee lookup gate, got %d calls", up.calls)
	}
}
