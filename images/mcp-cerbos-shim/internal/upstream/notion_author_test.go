package upstream

import (
	"context"
	"errors"
	"testing"
)

// notionAuthorFakeCaller is a ToolCaller stub for PageAuthoredByOperator's
// two-call sequence (fetch then creator-filtered search) -- fakeCaller
// (ancestry_test.go) only ever records a single canned response, which isn't
// enough here since fetch and search need independently controllable
// results/errors.
type notionAuthorFakeCaller struct {
	fetchText  string
	fetchErr   error
	searchText string
	searchErr  error

	fetchCalls    int
	searchCalls   int
	gotSearchArgs map[string]any
}

func (f *notionAuthorFakeCaller) CallTool(_ context.Context, tool string, args map[string]any) (*CallToolResult, error) {
	switch tool {
	case notionFetchTool:
		f.fetchCalls++
		if f.fetchErr != nil {
			return nil, f.fetchErr
		}
		return &CallToolResult{Content: []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}{{Type: "text", Text: f.fetchText}}}, nil
	case notionSearchTool:
		f.searchCalls++
		f.gotSearchArgs = args
		if f.searchErr != nil {
			return nil, f.searchErr
		}
		return &CallToolResult{Content: []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}{{Type: "text", Text: f.searchText}}}, nil
	default:
		return nil, errors.New("unexpected tool: " + tool)
	}
}

// realFetchEnvelope/realSearchEnvelope mirror the live wire shapes captured
// 2026-07-23 against the work cluster's real Notion connection (see
// PageAuthoredByOperator's doc comment).
const realFetchEnvelope = `{"metadata":{"type":"page"},"title":"⌨️ Chris - Hera UI Scratchpad","url":"https://app.notion.com/p/359588d8909f80dcbc07c6fd182261ea","text":"Here is the result of \"view\"...\n<page url=\"https://app.notion.com/p/359588d8909f80dcbc07c6fd182261ea\" icon=\"⌨️\">\n<ancestor-path></ancestor-path>\n<properties>\n{\"title\":\"Chris - Hera UI Scratchpad\"}\n</properties>\n<content>\nsome content\n</content>\n</page>"}`

const realSearchEnvelopeWithMatch = `{"text":"{\"results\":[{\"id\":\"359588d8-909f-80dc-bc07-c6fd182261ea\",\"title\":\"Chris - Hera UI Scratchpad\",\"url\":\"https://app.notion.com/p/359588d8909f80dcbc07c6fd182261ea?pvs=1\",\"type\":\"page\",\"highlight\":\"\",\"timestamp\":\"2026-05-13T00:04:00.000Z\"}],\"type\":\"workspace_search\"}"}`

const realSearchEnvelopeNoMatch = `{"text":"{\"results\":[{\"id\":\"1ea588d8-909f-80ed-b0ce-f38d0aeaf64f\",\"title\":\"Scratchpad\",\"url\":\"https://app.notion.com/p/1ea588d8909f80edb0cef38d0aeaf64f?pvs=1\",\"type\":\"page\",\"highlight\":\"\",\"timestamp\":\"2026-07-16T20:50:00.000Z\"}],\"type\":\"workspace_search\"}"}`

func TestPageAuthoredByOperator_MatchFoundInFilteredSearch(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchText: realFetchEnvelope, searchText: realSearchEnvelopeWithMatch}
	got, err := PageAuthoredByOperator(context.Background(), c, "359588d8909f80dcbc07c6fd182261ea", "1ded872b-594c-813d-aa2c-000228ba9671")
	if err != nil {
		t.Fatalf("PageAuthoredByOperator: %v", err)
	}
	if !got {
		t.Error("expected true when the fetched page id appears in the creator-filtered search results")
	}
	if c.fetchCalls != 1 || c.searchCalls != 1 {
		t.Errorf("expected exactly one fetch and one search call, got fetch=%d search=%d", c.fetchCalls, c.searchCalls)
	}
	if c.gotSearchArgs["query"] != "Chris - Hera UI Scratchpad" {
		t.Errorf("search query = %q, want the fetched page's <properties> title (not the envelope's emoji-prefixed title)", c.gotSearchArgs["query"])
	}
	filters, _ := c.gotSearchArgs["filters"].(map[string]any)
	ids, _ := filters["created_by_user_ids"].([]string)
	if len(ids) != 1 || ids[0] != "1ded872b-594c-813d-aa2c-000228ba9671" {
		t.Errorf("filters.created_by_user_ids = %v, want [operatorUserID]", ids)
	}
}

func TestPageAuthoredByOperator_NoMatchInFilteredSearch(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchText: realFetchEnvelope, searchText: realSearchEnvelopeNoMatch}
	got, err := PageAuthoredByOperator(context.Background(), c, "359588d8909f80dcbc07c6fd182261ea", "1ded872b-594c-813d-aa2c-000228ba9671")
	if err != nil {
		t.Fatalf("PageAuthoredByOperator: %v", err)
	}
	if got {
		t.Error("expected false when the fetched page id is absent from the creator-filtered search results")
	}
}

func TestPageAuthoredByOperator_FetchErrorFailsClosed(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchErr: errors.New("upstream timeout")}
	_, err := PageAuthoredByOperator(context.Background(), c, "somepageid", "someuser")
	if err == nil {
		t.Fatal("expected an error when the fetch call fails, got nil (would fail open)")
	}
	if c.searchCalls != 0 {
		t.Errorf("expected no search call after a fetch failure, got %d", c.searchCalls)
	}
}

func TestPageAuthoredByOperator_NoResolvableTitleFailsClosed(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchText: `{"metadata":{"type":"page"},"title":"","url":"x","text":"<page url=\"x\"><ancestor-path></ancestor-path><content>no properties block here</content></page>"}`}
	_, err := PageAuthoredByOperator(context.Background(), c, "somepageid", "someuser")
	if err == nil {
		t.Fatal("expected an error when no title can be resolved from the fetch result, got nil (would fail open)")
	}
	if c.searchCalls != 0 {
		t.Errorf("expected no search call when no title was resolved, got %d", c.searchCalls)
	}
}

func TestPageAuthoredByOperator_SearchErrorFailsClosed(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchText: realFetchEnvelope, searchErr: errors.New("upstream timeout")}
	_, err := PageAuthoredByOperator(context.Background(), c, "359588d8909f80dcbc07c6fd182261ea", "someuser")
	if err == nil {
		t.Fatal("expected an error when the search call fails, got nil (would fail open)")
	}
}

func TestPageAuthoredByOperator_MalformedSearchJSONFailsClosed(t *testing.T) {
	c := &notionAuthorFakeCaller{fetchText: realFetchEnvelope, searchText: `{not valid json`}
	_, err := PageAuthoredByOperator(context.Background(), c, "359588d8909f80dcbc07c6fd182261ea", "someuser")
	if err == nil {
		t.Fatal("expected an error for malformed search JSON, got nil (would fail open)")
	}
}
