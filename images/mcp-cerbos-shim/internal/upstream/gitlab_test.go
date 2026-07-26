package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestMRAuthor_ResolvesFromLiveShapeGuess(t *testing.T) {
	c := &fakeCaller{text: `{"iid":42,"author":{"username":"jchristensen"}}`}
	got, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err != nil {
		t.Fatalf("MRAuthor: %v", err)
	}
	if got != "jchristensen" {
		t.Errorf("MRAuthor = %q, want jchristensen", got)
	}
	if c.gotTool != "gitlab_get_merge_request" {
		t.Errorf("gotTool = %q, want gitlab_get_merge_request", c.gotTool)
	}
	if c.gotArgs["project_id"] != "148" || c.gotArgs["merge_request_iid"] != "42" {
		t.Errorf("gotArgs = %v, want project_id/merge_request_iid forwarded", c.gotArgs)
	}
}

// TestMRAuthor_SelectsBySourceBranchWhenNoIID: update_merge_request accepts
// either selector, so the lookup must follow whichever one the gated call used
// -- selecting a different MR than the one about to be written to would defeat
// the entire gate.
func TestMRAuthor_SelectsBySourceBranchWhenNoIID(t *testing.T) {
	c := &fakeCaller{text: `[{"iid":42,"author":{"username":"jchristensen"}}]`}
	got, err := MRAuthor(context.Background(), c, "148", "", "feature-x")
	if err != nil {
		t.Fatalf("MRAuthor: %v", err)
	}
	if got != "jchristensen" {
		t.Errorf("MRAuthor = %q, want jchristensen (from the one-element list shape)", got)
	}
	if c.gotArgs["source_branch"] != "feature-x" {
		t.Errorf("gotArgs[source_branch] = %v, want feature-x", c.gotArgs["source_branch"])
	}
	if _, has := c.gotArgs["merge_request_iid"]; has {
		t.Errorf("must not invent a merge_request_iid the caller never sent: %v", c.gotArgs)
	}
}

// TestMRAuthor_MultipleMatchesFailsClosed: two MRs sharing a source branch means
// the gate cannot tell which one the write lands on, and guessing would defeat
// the check.
func TestMRAuthor_MultipleMatchesFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `[{"iid":42,"author":{"username":"jchristensen"}},{"iid":43,"author":{"username":"jchristensen"}}]`}
	_, err := MRAuthor(context.Background(), c, "148", "", "feature-x")
	if err == nil {
		t.Fatal("expected an error when source_branch matches more than one MR, got nil (would gate the wrong MR)")
	}
}

func TestMRAuthor_EmptyListFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `[]`}
	_, err := MRAuthor(context.Background(), c, "148", "", "feature-x")
	if err == nil {
		t.Fatal("expected an error when no MR matches, got nil (would fail open)")
	}
}

func TestMRAuthor_MissingAuthorFieldFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"iid":42,"title":"some MR"}`}
	_, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err == nil {
		t.Fatal("expected an error when the result has no resolvable author username, got nil (would fail open)")
	}
}

func TestMRAuthor_EmptyUsernameFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"author":{"username":""}}`}
	_, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err == nil {
		t.Fatal("expected an error for an empty username, got nil (would fail open)")
	}
}

// TestMRAuthor_UnexpectedShapeFailsClosed is the safety net for the caveat in
// gitlab.go's own doc comment: author.username is inferred from GitLab's
// documented REST conventions, not verified against a live call to this MCP
// tool. A shape mismatch must deny every gated call, never let one through.
func TestMRAuthor_UnexpectedShapeFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"author":"jchristensen"}`}
	_, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err == nil {
		t.Fatal("expected an error for an author field that isn't a nested user object, got nil (would fail open)")
	}
}

func TestMRAuthor_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestMRAuthor_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := MRAuthor(context.Background(), c, "148", "42", "")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}

// errCanonicalLookup stands in for any upstream transport failure.
var errCanonicalLookup = errors.New("upstream exploded")

// CanonicalProjectID is what lets ${gitlabAllowedProjects} carry one value per
// project. The wrapper renders ids as JSON strings (verified live), but accept
// a bare number too rather than fail closed on a shape change that still
// unambiguously names the project.
func TestCanonicalProjectID_StringID(t *testing.T) {
	c := &fakeCaller{text: `{"id":"148","path_with_namespace":"jchristensen/vicegerent-agents"}`}
	got, err := CanonicalProjectID(context.Background(), c, "JChristensen%2FVicegerent-Agents")
	if err != nil {
		t.Fatalf("CanonicalProjectID: %v", err)
	}
	if got != "148" {
		t.Errorf("CanonicalProjectID = %q, want 148", got)
	}
}

func TestCanonicalProjectID_NumericID(t *testing.T) {
	c := &fakeCaller{text: `{"id":148,"path_with_namespace":"jchristensen/vicegerent-agents"}`}
	got, err := CanonicalProjectID(context.Background(), c, "jchristensen/vicegerent-agents")
	if err != nil {
		t.Fatalf("CanonicalProjectID: %v", err)
	}
	if got != "148" {
		t.Errorf("CanonicalProjectID = %q, want 148 from a bare JSON number", got)
	}
}

// The lookup must select the project the caller named, unmodified -- resolving
// a DIFFERENT project would hand Cerbos the wrong id to authorize.
func TestCanonicalProjectID_PassesProjectThrough(t *testing.T) {
	c := &fakeCaller{text: `{"id":"7"}`}
	if _, err := CanonicalProjectID(context.Background(), c, "some-group/project-7"); err != nil {
		t.Fatalf("CanonicalProjectID: %v", err)
	}
	if got := c.gotArgs["project_id"]; got != "some-group/project-7" {
		t.Errorf("lookup sent project_id=%v, want the caller's own value", got)
	}
	if c.gotTool != "gitlab_get_project" {
		t.Errorf("lookup called %q, want gitlab_get_project", c.gotTool)
	}
}

// Fail-closed: an unparseable result, a result with no id, and an upstream
// error must all error rather than yield a value the allowlist would match.
func TestCanonicalProjectID_FailsClosed(t *testing.T) {
	for name, c := range map[string]*fakeCaller{
		"not an object":  {text: `"nope"`},
		"no id field":    {text: `{"path_with_namespace":"a/b"}`},
		"upstream error": {err: errCanonicalLookup},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := CanonicalProjectID(context.Background(), c, "a/b"); err == nil {
				t.Errorf("expected an error so the caller fails closed")
			}
		})
	}
}
