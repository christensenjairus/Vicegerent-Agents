package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestGetIssueDetails_ResolvesTeamAndAssigneeFromLiveShape(t *testing.T) {
	// Live-verified 2026-07-25 shape: team and assignee are both bare top-level
	// display-name strings, and the stable user UUID rides in a SEPARATE
	// assigneeId field this parser deliberately ignores (no email anywhere).
	c := &fakeCaller{text: `{"id":"PROJ-1","title":"some issue","team":"HAHomelabs","assignee":"Jairus Christensen","assigneeId":"f60cb294-8107-4cbc-b0e3-d4180352849b"}`}
	team, assignee, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err != nil {
		t.Fatalf("GetIssueDetails: %v", err)
	}
	if team != "HAHomelabs" {
		t.Errorf("team = %q, want HAHomelabs", team)
	}
	if assignee != "Jairus Christensen" {
		t.Errorf("assignee = %q, want Jairus Christensen", assignee)
	}
	if c.gotTool != "linear_get_issue" {
		t.Errorf("gotTool = %q, want linear_get_issue", c.gotTool)
	}
	if c.gotArgs["id"] != "PROJ-1" {
		t.Errorf("gotArgs[id] = %v, want PROJ-1", c.gotArgs["id"])
	}
}

// TestGetIssueDetails_MissingAssigneeIsEmptyNotAnError proves the asymmetric
// contract: team is required (fails closed if absent), but assignee is
// genuinely optional on a real Linear issue -- an issue with no assignee at
// all must resolve cleanly, not error, or every unassigned issue's plain
// update would start getting denied.
func TestGetIssueDetails_MissingAssigneeIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PROJ-1","title":"some issue","team":"HAHomelabs"}`}
	team, assignee, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err != nil {
		t.Fatalf("GetIssueDetails: %v, want no error for a legitimately unassigned issue", err)
	}
	if team != "HAHomelabs" {
		t.Errorf("team = %q, want HAHomelabs", team)
	}
	if assignee != "" {
		t.Errorf("assignee = %q, want empty for an unassigned issue", assignee)
	}
}

func TestGetIssueDetails_NullAssigneeIsEmptyNotAnError(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PROJ-1","team":"HAHomelabs","assignee":null}`}
	_, assignee, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err != nil {
		t.Fatalf("GetIssueDetails: %v, want no error for an explicit null assignee", err)
	}
	if assignee != "" {
		t.Errorf("assignee = %q, want empty for a null assignee", assignee)
	}
}

// TestGetIssueDetails_UnparseableAssigneeShapeFailsClosed proves the other
// half of the asymmetric contract: an assignee field that DOES carry a
// value, but not in the assumed plain-string shape, must fail the whole
// lookup closed -- an issue WITH a real assignee this code fails to parse is
// a strictly more dangerous silent pass than one with none at all, and must
// not be confused with the legitimately-empty case above.
func TestGetIssueDetails_UnparseableAssigneeShapeFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PROJ-1","team":"HAHomelabs","assignee":{"id":"u1","name":"Jane"}}`}
	_, _, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err == nil {
		t.Fatal("expected an error for an unparseable (object-shaped) assignee, got nil (would silently treat a real assignee as unassigned)")
	}
}

func TestGetIssueDetails_MissingTeamFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"id":"PROJ-1","title":"some issue"}`}
	_, _, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err == nil {
		t.Fatal("expected an error when the result has no resolvable team, got nil (would fail open)")
	}
}

func TestGetIssueDetails_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, _, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestGetIssueDetails_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, _, err := GetIssueDetails(context.Background(), c, "PROJ-1")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
