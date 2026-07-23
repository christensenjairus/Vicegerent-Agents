package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestIssueAssignee_ResolvesFromEmailAddress(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{"assignee":{"accountId":"acc1","emailAddress":"jchristensen@moveworks.ai","displayName":"J Christensen"}}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "jchristensen@moveworks.ai" {
		t.Errorf("IssueAssignee = %q, want jchristensen@moveworks.ai", got)
	}
	if c.gotTool != "jira_jira_get_issue" {
		t.Errorf("gotTool = %q, want jira_jira_get_issue", c.gotTool)
	}
	if c.gotArgs["issue_key"] != "CHANGE-1" {
		t.Errorf("gotArgs[issue_key] = %v, want CHANGE-1", c.gotArgs["issue_key"])
	}
}

// TestIssueAssignee_FallsBackToDisplayNameWhenEmailSuppressed covers the
// known Jira Cloud footgun: an org can suppress emailAddress from API
// responses per-user privacy settings, independent of whether the ticket
// really is assigned to the configured user.
func TestIssueAssignee_FallsBackToDisplayNameWhenEmailSuppressed(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{"assignee":{"accountId":"acc1","displayName":"J Christensen"}}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "J Christensen" {
		t.Errorf("IssueAssignee = %q, want J Christensen (fallback from suppressed emailAddress)", got)
	}
}

func TestIssueAssignee_FallsBackToAccountIDWhenOnlyThatIsPresent(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{"assignee":{"accountId":"acc1"}}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v", err)
	}
	if got != "acc1" {
		t.Errorf("IssueAssignee = %q, want acc1 (fallback to accountId)", got)
	}
}

// TestIssueAssignee_NullAssigneeIsEmptyNotAnError proves the asymmetric
// contract mirroring linear.go's GetIssueDetails: a genuinely unassigned
// Jira issue must resolve cleanly, not error.
func TestIssueAssignee_NullAssigneeIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{"assignee":null}}`}
	got, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err != nil {
		t.Fatalf("IssueAssignee: %v, want no error for an unassigned issue", err)
	}
	if got != "" {
		t.Errorf("IssueAssignee = %q, want empty for an unassigned issue", got)
	}
}

func TestIssueAssignee_AbsentAssigneeFieldIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"fields":{}}`}
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
	c := &fakeCaller{text: `{"fields":{"assignee":{}}}`}
	_, err := IssueAssignee(context.Background(), c, "CHANGE-1")
	if err == nil {
		t.Fatal("expected an error for an assignee object with no resolvable identifier, got nil (would silently treat a real assignee as unassigned)")
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
