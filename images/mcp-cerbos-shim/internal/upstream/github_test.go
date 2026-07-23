package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestPRAuthor_ResolvesFromLiveShapeGuess(t *testing.T) {
	c := &fakeCaller{text: `{"number":42,"user":{"login":"christensenjairus"}}`}
	got, err := PRAuthor(context.Background(), c, "christensenjairus", "vicegerent-agents", 42)
	if err != nil {
		t.Fatalf("PRAuthor: %v", err)
	}
	if got != "christensenjairus" {
		t.Errorf("PRAuthor = %q, want christensenjairus", got)
	}
	if c.gotTool != "github_pull_request_read" {
		t.Errorf("gotTool = %q, want github_pull_request_read", c.gotTool)
	}
	if c.gotArgs["owner"] != "christensenjairus" || c.gotArgs["repo"] != "vicegerent-agents" {
		t.Errorf("gotArgs = %v, want owner/repo forwarded", c.gotArgs)
	}
	if c.gotArgs["pullNumber"] != float64(42) {
		t.Errorf("gotArgs[pullNumber] = %v, want 42", c.gotArgs["pullNumber"])
	}
	if c.gotArgs["method"] != "get" {
		t.Errorf("gotArgs[method] = %v, want get", c.gotArgs["method"])
	}
}

func TestPRAuthor_MissingUserFieldFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"number":42,"title":"some PR"}`}
	_, err := PRAuthor(context.Background(), c, "someowner", "somerepo", 42)
	if err == nil {
		t.Fatal("expected an error when the result has no resolvable author login, got nil (would fail open)")
	}
}

func TestPRAuthor_EmptyLoginFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{"user":{"login":""}}`}
	_, err := PRAuthor(context.Background(), c, "someowner", "somerepo", 42)
	if err == nil {
		t.Fatal("expected an error for an empty login, got nil (would fail open)")
	}
}

func TestPRAuthor_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := PRAuthor(context.Background(), c, "someowner", "somerepo", 42)
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestPRAuthor_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := PRAuthor(context.Background(), c, "someowner", "somerepo", 42)
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
