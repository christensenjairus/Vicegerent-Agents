package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestIssueAssignee_ResolvesFromEmail(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":{"account_id":"acc1","email":"person@example.com","display_name":"J Smith","name":"J Smith"}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "person@example.com" {
		t.Errorf("IssueAssignee = %q, want person@example.com", got)
	}
	if c.gotTool != "jira_jira_get_issue" {
		t.Errorf("gotTool = %q, want jira_jira_get_issue", c.gotTool)
	}
	if c.gotArgs["issue_key"] != "CHANGE-1" {
		t.Errorf("gotArgs[issue_key] = %v, want CHANGE-1", c.gotArgs["issue_key"])
	}
}

// TestIssueAssignee_FallsBackToDisplayNameWhenEmailSuppressed covers the
// known Jira Cloud footgun: an org can suppress email from API responses
// per-user privacy settings, independent of whether the ticket really is
// assigned to the configured user.
func TestIssueAssignee_FallsBackToDisplayNameWhenEmailSuppressed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":{"account_id":"acc1","display_name":"J Smith"}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "J Smith" {
		t.Errorf("IssueAssignee = %q, want J Smith (fallback from suppressed email)", got)
	}
}

func TestIssueAssignee_FallsBackToAccountIDWhenOnlyThatIsPresent(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":{"account_id":"acc1"}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "acc1" {
		t.Errorf("IssueAssignee = %q, want acc1 (fallback to account_id)", got)
	}
}

// TestIssueAssignee_NullAssigneeIsEmptyNotAnError proves the asymmetric
// contract mirroring linear.go's GetIssueDetails: a genuinely unassigned
// Jira issue must resolve cleanly, not error.
func TestIssueAssignee_NullAssigneeIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":null}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v, want no error for an unassigned issue", err)
	}
	if got != "" {
		t.Errorf("IssueAssignee = %q, want empty for an unassigned issue", got)
	}
}

func TestIssueAssignee_AbsentAssigneeFieldIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1"}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v, want no error when the assignee field is absent entirely", err)
	}
	if got != "" {
		t.Errorf("IssueAssignee = %q, want empty", got)
	}
}

// TestIssueAssignee_AssigneeObjectWithNoIdentifierFailsClosed proves the
// other half of the asymmetric contract: an assignee object that DOES exist
// but carries none of the three known identifier fields must fail closed --
// an issue WITH a real assignee this code can't identify is a strictly more
// dangerous silent pass than one with none at all.
func TestIssueAssignee_AssigneeObjectWithNoIdentifierFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":{}}`}
	_, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err == nil {
		t.Fatal("expected an error for an assignee object with no resolvable identifier, got nil (would silently treat a real assignee as unassigned)")
	}
}

// TestIssueAssignee_UnassignedSentinelIsEmptyNotARealUser guards against a
// second, distinct silent-misparse bug found live testing !636/!637's own
// unassigned-issue gate against a real ticket: mcp-atlassian represents a
// genuinely unassigned issue as {"display_name":"Unassigned"}, not a JSON
// null -- so, before this fix, the DisplayName fallback below matched the
// sentinel and returned "Unassigned" as if it were a real user, which made
// deny-assignee-outside-allowed fire instead of deny-write-unassigned-issue.
// Same net deny, wrong rule, wrong reason surfaced -- the mirror image of
// TestIssueAssignee_RejectsLegacyRESTShape below (that one: a real assignee
// silently resolved as unassigned; this one: "unassigned" silently resolved
// as a real assignee).
func TestIssueAssignee_UnassignedSentinelIsEmptyNotARealUser(t *testing.T) {
	c := &fakeCaller{text: `{"id":"1","key":"CHANGE-1","assignee":{"display_name":"Unassigned"}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v, want no error for the Unassigned sentinel", err)
	}
	if got != "" {
		t.Errorf("IssueAssignee = %q, want empty: mcp-atlassian's {display_name:Unassigned} sentinel is not a real user", got)
	}
}

// TestIssueAssignee_RejectsLegacyRESTShape guards against regressing back to
// the raw REST fields.assignee.{accountId,emailAddress,displayName} shape
// this package originally (and incorrectly) assumed -- that mismatch
// silently resolved every assigned issue as unassigned (see jira.go's
// jiraIssueResult doc comment). A result in that shape resolves as
// unassigned here too (there's no top-level "assignee" key to read),
// indistinguishable from a real absent-assignee result -- exactly why the
// original bug was silent rather than an error.
func TestIssueAssignee_RejectsLegacyRESTShape(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{"assignee":{"accountId":"acc1","emailAddress":"person@example.com","displayName":"J Smith"}}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "" {
		t.Errorf("IssueAssignee = %q, want empty: the legacy fields.assignee shape has no top-level assignee key", got)
	}
}

func TestIssueAssignee_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestIssueAssignee_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
