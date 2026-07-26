package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// notionSearchTool is the vMCP tool name for Notion's workspace search --
// backend-prefixed the same way mapping.yaml keys its tools
// (notion_notion-search). Like notion_notion-fetch (see ancestry.go), this
// stays fully UNMAPPED in Cerbos so this gate's own re-entrant lookup call
// can't recurse into itself.
const notionSearchTool = "notion_notion-search"

// notionPropertiesRe extracts the JSON object from a notion-fetch result's
// <properties> block. The inner title is used because the envelope title can
// include an emoji icon and therefore may not match notion-search results.
var notionPropertiesRe = regexp.MustCompile(`(?s)<properties>\s*(\{.*?\})\s*</properties>`)

// extractPropertiesJSON returns the raw JSON object inside fetchText's
// <properties> block, or false if no such block is found.
func extractPropertiesJSON(fetchText string) (string, bool) {
	m := notionPropertiesRe.FindStringSubmatch(fetchText)
	if m == nil {
		return "", false
	}
	return m[1], true
}

// notionSearchEnvelope is the outer JSON object the real notion-search tool
// wraps its actual result list in -- the same double-JSON-envelope shape as
// notion-fetch (see ancestry.go's notionFetchEnvelope doc comment): the
// CallToolResult.Content[].Text plumbing only unmarshals the OUTER JSON-RPC
// envelope, so this inner "text" field must be decoded again before the
// result list is usable.
type notionSearchEnvelope struct {
	Text string `json:"text"`
}

type notionSearchResult struct {
	ID    string `json:"id"`
	Title string `json:"title"`
}

type notionSearchResults struct {
	Results []notionSearchResult `json:"results"`
}

// extractPageTitle recovers a fetched page's title from its (already
// envelope-unwrapped) notion-fetch text, reading the <properties> block's
// own "title" field rather than the envelope's top-level title -- see
// notionPropertiesRe's doc comment for why. Returns "" if no
// properties/title block is found.
func extractPageTitle(fetchText string) string {
	raw, ok := extractPropertiesJSON(fetchText)
	if !ok {
		return ""
	}
	var props struct {
		Title string `json:"title"`
	}
	if err := json.Unmarshal([]byte(raw), &props); err != nil {
		return ""
	}
	return props.Title
}

// ownerPropertyMentionsOperator reports whether propertiesJSON (the raw
// <properties> block of a fetched page) has a property named "Owner"
// (case-insensitive -- database schemas in this workspace capitalize
// property names inconsistently) whose value mentions operatorUserID.
//
// Notion renders person properties as mention tags containing user:// UUIDs.
// Inspecting the raw JSON supports both single- and multi-person values. A
// missing or differently named Owner property returns false and lets
// PageAuthoredByOperator fall back to its creator-filtered search.
func ownerPropertyMentionsOperator(propertiesJSON, operatorUserID string) bool {
	var props map[string]json.RawMessage
	if err := json.Unmarshal([]byte(propertiesJSON), &props); err != nil {
		return false
	}
	needle := "user://" + operatorUserID
	for key, raw := range props {
		if strings.EqualFold(key, "owner") && strings.Contains(string(raw), needle) {
			return true
		}
	}
	return false
}

// PageAuthoredByOperator reports whether operatorUserID owns pageID through
// the page's Owner property or created the page. notion-fetch does not expose
// creator metadata, so the creator check searches for the fetched title with
// filters.created_by_user_ids and confirms that pageID is in the results.
//
// This creator-search is a SEMANTIC search match, not an exact field
// comparison -- a false negative (an operator-authored page doesn't rank in
// the filtered results) denies a legitimate write, which is the
// fail-closed-safe direction; it can never produce a false positive (a
// non-operator page being reported as operator-authored), since the filter
// itself is a hard server-side exclusion, not a ranking signal. This is the
// Owner-property check also permits pages reassigned after creation without
// an extra network call. Returns an error
// (fail closed) only on an actual lookup failure -- a fetch/search error, or
// a page with no resolvable title to search on -- never on the Owner-property
// check finding nothing, since that's just "this fallback doesn't apply
// here," not a failure.
func PageAuthoredByOperator(ctx context.Context, client ToolCaller, pageID, operatorUserID string) (bool, error) {
	fetchResult, err := client.CallTool(ctx, notionFetchTool, map[string]any{"id": pageID})
	if err != nil {
		return false, fmt.Errorf("notion author lookup for page %q: fetch: %w", pageID, err)
	}
	fetchText := extractNotionFetchText(fetchResult.Text())

	if propsJSON, ok := extractPropertiesJSON(fetchText); ok && ownerPropertyMentionsOperator(propsJSON, operatorUserID) {
		return true, nil
	}

	title := extractPageTitle(fetchText)
	if title == "" {
		return false, fmt.Errorf("notion author lookup for page %q: could not resolve a title to search on", pageID)
	}

	searchResult, err := client.CallTool(ctx, notionSearchTool, map[string]any{
		"query":                title,
		"query_type":           "internal",
		"page_size":            25,
		"max_highlight_length": 0,
		"filters":              map[string]any{"created_by_user_ids": []string{operatorUserID}},
	})
	if err != nil {
		return false, fmt.Errorf("notion author lookup for page %q: creator-filtered search: %w", pageID, err)
	}
	raw := searchResult.Text()
	var env notionSearchEnvelope
	if err := json.Unmarshal([]byte(raw), &env); err == nil && env.Text != "" {
		raw = env.Text
	}
	var results notionSearchResults
	if err := json.Unmarshal([]byte(raw), &results); err != nil {
		return false, fmt.Errorf("notion author lookup for page %q: malformed search result: %w", pageID, err)
	}
	target := normalizeID(pageID)
	for _, r := range results.Results {
		if normalizeID(r.ID) == target {
			return true, nil
		}
	}
	return false, nil
}
