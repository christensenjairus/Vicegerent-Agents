package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
)

// notionSearchTool is the vMCP tool name for Notion's workspace search --
// backend-prefixed the same way mapping.yaml keys its tools
// (notion_notion-search). Like notion_notion-fetch (see ancestry.go), this
// stays fully UNMAPPED in Cerbos so this gate's own re-entrant lookup call
// can't recurse into itself.
const notionSearchTool = "notion_notion-search"

// notionPropertiesTitleRe pulls a page's title out of the <properties> JSON
// block inside a notion-fetch result's flattened markdown -- NOT the outer
// envelope's own top-level "title" field, which inconsistently prepends the
// page's icon character when (and only when) that icon happens to be a
// literal emoji. Live-verified against two real pages (2026-07-23): a page
// with icon="⌨️" got envelope title "⌨️ Chris - Hera UI Scratchpad", while a
// page with icon="icons/pencil_lightgray" (an icon ASSET reference, not an
// emoji) got a clean "Scratchpad" with no prefix at all. The inner
// <properties> block's title has neither quirk and matches notion-search's
// own returned title field exactly in both cases -- which is exactly what
// this package needs to match a fetched page against search results.
var notionPropertiesTitleRe = regexp.MustCompile(`(?s)<properties>\s*(\{.*?\})\s*</properties>`)

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
// notionPropertiesTitleRe's doc comment for why. Returns "" if no
// properties/title block is found.
func extractPageTitle(fetchText string) string {
	m := notionPropertiesTitleRe.FindStringSubmatch(fetchText)
	if m == nil {
		return ""
	}
	var props struct {
		Title string `json:"title"`
	}
	if err := json.Unmarshal([]byte(m[1]), &props); err != nil {
		return ""
	}
	return props.Title
}

// PageAuthoredByOperator reports whether pageID was created by
// operatorUserID, via a title fetch followed by a creator-filtered workspace
// search. notion-fetch's own output exposes NO author/creator field
// whatsoever -- live-verified 2026-07-23 against two different real pages,
// one confirmed authored by someone else: the returned envelope and its
// inner <page> markdown carry metadata/title/url/text and the full page
// content, but nothing resembling created_by/author/creator anywhere -- so
// there is no direct field to read the way PRAuthor/IssueAssignee do for
// their own backends. notion-search's `filters.created_by_user_ids` IS a
// genuine, deterministic per-item creator filter though (also live-verified,
// via a controlled A/B test: searching a page's exact title with the filter
// set to a user who did NOT create it excludes that page from the result
// list entirely, while the identical query with no filter returns it as the
// #1 near-exact-title match) -- so this fetches the page's title, then
// repeats that exact title as a search query filtered to operatorUserID, and
// reports whether pageID itself appears among the (filtered) results.
//
// This is a SEMANTIC search match, not an exact field comparison -- a false
// negative (an operator-authored page doesn't rank in the filtered results)
// denies a legitimate write, which is the fail-closed-safe direction; it can
// never produce a false positive (a non-operator page being reported as
// operator-authored), since the filter itself is a hard server-side
// exclusion, not a ranking signal. Returns an error (fail closed) only on an
// actual lookup failure -- a fetch/search error, or a page with no
// resolvable title to search on.
func PageAuthoredByOperator(ctx context.Context, client ToolCaller, pageID, operatorUserID string) (bool, error) {
	fetchResult, err := client.CallTool(ctx, notionFetchTool, map[string]any{"id": pageID})
	if err != nil {
		return false, fmt.Errorf("notion author lookup for page %q: fetch: %w", pageID, err)
	}
	title := extractPageTitle(extractNotionFetchText(fetchResult.Text()))
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
