package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns an Alertmanager createSilence call into
// the alertmanager_silence resource Cerbos caps/denies; the deny *decision*
// itself is proven by defs/alertmanager_test.yaml. createSilence's own
// createdBy arg is force-overridden to ${alertmanagerCreatedBy} regardless of
// what the caller sends (mapping.yaml's `force` block) -- proven below
// alongside the duration-cap tests.
//
// deleteSilence is now MAPPED (unlike createSilence's siblings above, it used
// to be unmapped -- see resource_alertmanager.yaml's history) with a
// live-resolved owner gate: the shim resolves the target silence's real
// createdBy via a getSilences lookup (FAKE upstream here, no network) and
// forwards it to Cerbos as the createdBy attr, same shape as the GitHub
// PR-author gate (github_pr_author_deployed_test.go). These tests prove that
// gate's wiring -- the allow/deny decision itself (createdBy != allowed
// value) is proven separately by defs/alertmanager_test.yaml.

// alertmanagerSilencesResultOwnCreator/OtherCreator mirror getSilences'
// inferred v2-REST-API result shape (see upstream/alertmanager.go's own
// caveat: NOT verified against a live call).
const alertmanagerSilencesResultOwnCreator = `[{"id":"6f9d3a2e-1234-4567-8901-abcdef012345","createdBy":"vicegerent-work"}]`
const alertmanagerSilencesResultOtherCreator = `[{"id":"7a0e4b3f-2345-5678-9012-bcdef0123456","createdBy":"someoneelse"}]`
const alertmanagerSilencesResultEmpty = `[]`

func newAlertmanagerServer(t *testing.T, d *stubDecider, up upstream.ToolCaller) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	if up == nil {
		return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithAlertmanagerSilenceOwner(up))
}

func TestDeployedAlertmanagerMapping_DeleteSilenceResolvesAndForwardsOwnCreator(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: alertmanagerSilencesResultOwnCreator}
	s := newAlertmanagerServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "6f9d3a2e-1234-4567-8901-abcdef012345"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: silence was created by the allowed identity")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one getSilences lookup, got %d", up.calls)
	}
	if up.gotTool != "alertmanager_getSilences" {
		t.Errorf("lookup used tool %q, want alertmanager_getSilences", up.gotTool)
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if d.gotType != "alertmanager_silence" || d.gotAct != "delete" {
		t.Errorf("Cerbos saw resource=%q action=%q, want alertmanager_silence/delete", d.gotType, d.gotAct)
	}
	if got, _ := d.gotAttr["createdBy"].(string); got != "vicegerent-work" {
		t.Errorf("Cerbos saw createdBy=%q, want the resolved creator vicegerent-work", got)
	}
}

// TestDeployedAlertmanagerMapping_DeleteSilenceOnOtherCreatorResolvesAndForwards
// proves the GATE's half of the contract: it resolves the silence's real
// createdBy (someoneelse, not this machine's identity) and hands that exact
// value to Cerbos as createdBy. The actual allow/deny decision for a
// non-matching createdBy is Cerbos policy's job, already covered by
// defs/alertmanager_test.yaml's deny-not-own-silence case -- this test uses
// stubDecider (a fixed verdict, not real policy logic) only to confirm what
// the gate SENDS, not what Cerbos DECIDES.
func TestDeployedAlertmanagerMapping_DeleteSilenceOnOtherCreatorResolvesAndForwards(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: alertmanagerSilencesResultOtherCreator}
	s := newAlertmanagerServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "7a0e4b3f-2345-5678-9012-bcdef0123456"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in alertmanager_test.yaml)")
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if got, _ := d.gotAttr["createdBy"].(string); got != "someoneelse" {
		t.Errorf("Cerbos saw createdBy=%q, want the resolved (non-matching) creator someoneelse", got)
	}
}

func TestDeployedAlertmanagerMapping_DeleteSilenceLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newAlertmanagerServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "6f9d3a2e-1234-4567-8901-abcdef012345"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the getSilences lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedAlertmanagerMapping_DeleteSilenceNotFoundFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: alertmanagerSilencesResultEmpty}
	s := newAlertmanagerServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "missing-silence"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed): getSilences result had no matching silence")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedAlertmanagerMapping_DeleteSilenceUnconfiguredGateFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	// No WithAlertmanagerSilenceOwner: production's main.go always wires it,
	// so reaching here unconfigured means a broken deploy, not a license to
	// allow an unscoped silence delete through -- same posture as the GitHub
	// PR-author gate's unconfigured-gate test.
	s := newAlertmanagerServer(t, d, nil)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "6f9d3a2e-1234-4567-8901-abcdef012345"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny with the silence-owner gate unconfigured (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached with the gate unconfigured, got %d calls", d.calls)
	}
}

// TestDeployedAlertmanagerMapping_GetAlertsDoesNotTriggerSilenceOwnerGate
// proves a DIFFERENT Alertmanager tool sharing no resource/action with
// deleteSilence is untouched by this gate.
func TestDeployedAlertmanagerMapping_GetAlertsDoesNotTriggerSilenceOwnerGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newAlertmanagerServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_getAlerts",
			map[string]any{"filterLabel": "severity=critical"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny=%v mutated=%v", isDeny(res), isMutated(res))
	}
	if up.calls != 0 {
		t.Errorf("getAlerts must not trigger the silence-owner lookup gate, got %d calls", up.calls)
	}
}

func TestDeployedAlertmanagerMapping_CreateSilenceReachesCerbosWithDurationSeconds(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_createSilence",
			map[string]any{"alertName": "HighMemoryUsage", "duration": "2h"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	// Every allowed createSilence is mutated (force stamps createdBy), never a
	// plain pass -- see TestDeployedAlertmanagerMapping_CreateSilenceForcesCreatedBy.
	if !isMutated(res) {
		t.Fatalf("expected mutated (Cerbos allows + force applies), got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "alertmanager_silence" {
		t.Errorf("resourceType = %q, want alertmanager_silence", d.gotType)
	}
	if d.gotAttr["durationSeconds"] != "7200" {
		t.Errorf("attr.durationSeconds = %q, want 7200 (2h)", d.gotAttr["durationSeconds"])
	}
}

func TestDeployedAlertmanagerMapping_CreateSilenceOmittedDurationDefaultsToTwoHours(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_createSilence",
			map[string]any{"alertName": "HighMemoryUsage"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isMutated(res) {
		t.Fatalf("expected mutated (Cerbos allows + force applies), got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	if d.gotAttr["durationSeconds"] != "7200" {
		t.Errorf("attr.durationSeconds = %q, want 7200 (default 2h when duration omitted)", d.gotAttr["durationSeconds"])
	}
}

// TestDeployedAlertmanagerMapping_CreateSilenceForcesCreatedBy proves
// createSilence's force:{createdBy: ${alertmanagerCreatedBy}} mapping
// overrides whatever the caller sends -- the OTHER half of the two-halves
// design (see resource_alertmanager.yaml): whatever this stamps is exactly
// what deleteSilence's owner gate later checks against.
func TestDeployedAlertmanagerMapping_CreateSilenceForcesCreatedBy(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_createSilence",
			map[string]any{"alertName": "HighMemoryUsage", "duration": "2h", "createdBy": "whatever-the-caller-sent"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isMutated(res) {
		t.Fatalf("expected a mutated (force createdBy) result, got pass=%v deny=%v", isPass(res), isDeny(res))
	}
	_, args := decodeMutated(t, res)
	// mapping.yaml is loaded raw here (no Flux postBuild.substituteFrom, which
	// only runs at cluster reconcile time) -- the forced value is still the
	// literal ${alertmanagerCreatedBy} token, not a real cluster-var value.
	// That's still enough to prove the override happened: it must not be the
	// caller-supplied value.
	if args["createdBy"] != "${alertmanagerCreatedBy}" {
		t.Errorf("forced arg createdBy = %v, want the literal ${alertmanagerCreatedBy} token (Flux substitutes this at reconcile time, not here)", args["createdBy"])
	}
}

// getAlerts is mapped (unlike deleteSilence above) to alertmanager_alert_query
// carrying filterLabel, so Cerbos's deny-getAlerts-missing-filter rule
// (defs/resource_alertmanager_alert_query.yaml) can actually see and enforce
// it. These tests prove the wiring, not the policy decision itself -- that's
// covered by defs/alertmanager_alert_query_test.yaml.

func TestDeployedAlertmanagerMapping_GetAlertsReachesCerbosWithFilterLabel(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_getAlerts",
			map[string]any{"filterLabel": "severity=critical"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "alertmanager_alert_query" {
		t.Errorf("resourceType = %q, want alertmanager_alert_query", d.gotType)
	}
	if d.gotAttr["filterLabel"] != "severity=critical" {
		t.Errorf("attr.filterLabel = %q, want severity=critical", d.gotAttr["filterLabel"])
	}
}

func TestDeployedAlertmanagerMapping_GetAlertsMissingFilterLabelIsDeniedByShippedPolicy(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// stubDecider stands in for Cerbos here (this test proves the shim's
	// wiring, not Cerbos's own decision), so denial is asserted directly
	// against the attr the shim would send: an empty filterLabel is exactly
	// the shape defs/resource_alertmanager_alert_query.yaml's
	// deny-getAlerts-missing-filter rule matches on.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_getAlerts",
			map[string]any{})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isPass(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotAttr["filterLabel"] != "" {
		t.Errorf("attr.filterLabel = %q, want empty string when filterLabel is omitted", d.gotAttr["filterLabel"])
	}
}
