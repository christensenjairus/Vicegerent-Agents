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

// notionPropertiesRe pulls the raw JSON object out of the <properties> block
// inside a notion-fetch result's flattened markdown -- shared by
// extractPageTitle (title lookup, keyed on "title") and
// ownerPropertyMentionsOperator (Owner-property fallback, keyed on "Owner")
// below, since both need the same raw properties blob and differ only in
// which key they read out of it. NOT the outer envelope's own top-level
// "title" field, which inconsistently prepends the page's icon character
// when (and only when) that icon happens to be a literal emoji. Live-verified
// against two real pages (2026-07-23): a page with icon="⌨️" got envelope
// title "⌨️ Some Notes Page", while a page with
// icon="icons/pencil_lightgray" (an icon ASSET reference, not an emoji) got a
// clean "Scratchpad" with no prefix at all. The inner <properties> block's
// title has neither quirk and matches notion-search's own returned title
// field exactly in both cases -- which is exactly what this package needs to
// match a fetched page against search results.
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
// Only a page that is itself a row in a database with an "Owner"-named
// person property has anything to check here -- an ordinary non-database
// page's properties block carries only "title" (live-verified 2026-07-24
// against this shim's own Scratchpad page) and never matches, falling
// through to PageAuthoredByOperator's existing creator-search check
// unaffected.
//
// A Notion "person" property is rendered inside <properties> as a JSON
// array of mention tags, e.g. ["<mention-user
// url=\"user://<uuid>\"></mention-user>"] -- live-verified 2026-07-24 against
// a real person property in this workspace named something other than
// "Owner" (Notion has no property literally named "Owner" in any page this
// lookup was tested against, but "person"-typed properties all share this
// same rendering regardless of the column's name). This checks a raw
// substring match for `user://<operatorUserID>` against the property's raw
// JSON value rather than unmarshaling into a specific shape (string vs
// []string) -- that covers both a single-person and multi-person property
// alike without needing to know which shape a given database uses. A false
// negative here (a genuinely-owned page whose Owner property doesn't match,
// e.g. because the workspace's property is named something else entirely)
// just means this fallback doesn't help and PageAuthoredByOperator falls
// through to its creator-search check; a false positive would require the
// exact operator UUID to appear as a substring of an unrelated property
// value, which isn't realistically possible by accident.
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

// PageAuthoredByOperator reports whether pageID was created by
// operatorUserID, OR whether its own "Owner" property (if it has one)
// mentions operatorUserID, via a title fetch followed by a creator-filtered
// workspace search. notion-fetch's own output exposes NO author/creator
// field whatsoever -- live-verified 2026-07-23 against two different real
// pages, one confirmed authored by someone else: the returned envelope and
// its inner <page> markdown carry metadata/title/url/text and the full page
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
// This creator-search is a SEMANTIC search match, not an exact field
// comparison -- a false negative (an operator-authored page doesn't rank in
// the filtered results) denies a legitimate write, which is the
// fail-closed-safe direction; it can never produce a false positive (a
// non-operator page being reported as operator-authored), since the filter
// itself is a hard server-side exclusion, not a ranking signal. This is the
// real gap the Owner-property OR-fallback closes: a page an operator didn't
// CREATE but is nonetheless the assigned Owner of (e.g. a page someone else
// created on their behalf, or a database row reassigned after creation) was
// previously always denied, even though the operator is its legitimate
// current owner -- ownerPropertyMentionsOperator checks that case first,
// directly against the already-fetched properties block (no extra network
// call), before falling through to the creator search. Returns an error
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
