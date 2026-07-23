package upstream

import (
	"context"
	"errors"
	"testing"
)

func TestSilenceCreatedBy_ResolvesFromLiveShapeGuess(t *testing.T) {
	c := &fakeCaller{text: `[{"id":"other-silence","createdBy":"someoneelse"},{"id":"target-silence","createdBy":"vicegerent-personal"}]`}
	got, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", c, "target-silence")
	if err != nil {
		t.Fatalf("SilenceCreatedBy: %v", err)
	}
	if got != "vicegerent-personal" {
		t.Errorf("SilenceCreatedBy = %q, want vicegerent-personal", got)
	}
	if c.gotTool != "alertmanager_getSilences" {
		t.Errorf("gotTool = %q, want alertmanager_getSilences", c.gotTool)
	}
}

func TestSilenceCreatedBy_NotFoundFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `[{"id":"other-silence","createdBy":"someoneelse"}]`}
	_, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", c, "missing-silence")
	if err == nil {
		t.Fatal("expected an error when the silence isn't in the getSilences result, got nil (would fail open)")
	}
}

func TestSilenceCreatedBy_EmptyCreatedByFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `[{"id":"target-silence","createdBy":""}]`}
	_, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", c, "target-silence")
	if err == nil {
		t.Fatal("expected an error for an empty createdBy, got nil (would fail open)")
	}
}

func TestSilenceCreatedBy_MalformedJSONFailsClosed(t *testing.T) {
	c := &fakeCaller{text: `{not valid json`}
	_, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", c, "target-silence")
	if err == nil {
		t.Fatal("expected an error for malformed JSON, got nil (would fail open)")
	}
}

func TestSilenceCreatedBy_LookupFailurePropagates(t *testing.T) {
	c := &fakeCaller{err: errors.New("connection refused")}
	_, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", c, "target-silence")
	if err == nil {
		t.Fatal("expected the underlying CallTool error to propagate, got nil")
	}
}
