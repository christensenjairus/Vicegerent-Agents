package upstream

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// newTextResult builds the single-text-block result shape every lookup parses.
func newTextResult(text string) *CallToolResult {
	return &CallToolResult{Content: []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}{{Type: "text", Text: text}}}
}

// countingCaller is a ToolCaller stub whose result CHANGES on every call
// ("r1", "r2", ...) -- unlike fakeCaller's fixed text, that makes a cache hit
// visible in the returned value and not only in the call count, so a test
// can't pass by accidentally re-calling upstream.
type countingCaller struct {
	mu    sync.Mutex
	calls int
	err   error
	text  func(n int) string // nil => "rN"
}

func (c *countingCaller) CallTool(_ context.Context, _ string, _ map[string]any) (*CallToolResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls++
	if c.err != nil {
		return nil, c.err
	}
	if c.text != nil {
		return newTextResult(c.text(c.calls)), nil
	}
	return newTextResult(fmt.Sprintf("r%d", c.calls)), nil
}

func (c *countingCaller) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.calls
}

// testCache returns a cache on a clock the test drives, plus the knob to
// advance it -- expiry is tested by moving time, never by sleeping.
func testCache() (*Cache, func(time.Duration)) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c := NewCache()
	var mu sync.Mutex
	c.now = func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		return now
	}
	return c, func(d time.Duration) {
		mu.Lock()
		defer mu.Unlock()
		now = now.Add(d)
	}
}

func TestCached_SecondIdenticalLookupIsServedFromCache(t *testing.T) {
	inner := &countingCaller{}
	cache, _ := testCache()
	client := Cached(inner, cache)

	args := map[string]any{"project_id": "group/project"}
	first, err := client.CallTool(context.Background(), gitlabGetProjectTool, args)
	if err != nil {
		t.Fatalf("first CallTool: %v", err)
	}
	second, err := client.CallTool(context.Background(), gitlabGetProjectTool, args)
	if err != nil {
		t.Fatalf("second CallTool: %v", err)
	}
	if got := inner.count(); got != 1 {
		t.Errorf("upstream called %d times, want 1", got)
	}
	if first.Text() != second.Text() {
		t.Errorf("cached result %q differs from original %q", second.Text(), first.Text())
	}
}

// TestCached_UnknownToolIsNotCached keeps authorization lookups fail-safe by
// default. A newly added gate must make an explicit TTL decision before its
// result can become stale; merely passing through ToolCaller is not enough.
func TestCached_UnknownToolIsNotCached(t *testing.T) {
	inner := &countingCaller{}
	cache, _ := testCache()
	client := Cached(inner, cache)

	const unknownTool = "future_list_based_authorization_lookup"
	args := map[string]any{"id": "resource-a"}
	first, err := client.CallTool(context.Background(), unknownTool, args)
	if err != nil {
		t.Fatalf("first CallTool: %v", err)
	}
	second, err := client.CallTool(context.Background(), unknownTool, args)
	if err != nil {
		t.Fatalf("second CallTool: %v", err)
	}
	if got := inner.count(); got != 2 {
		t.Errorf("unknown tool called upstream %d times, want 2 (unknown tools must bypass the cache)", got)
	}
	if first.Text() == second.Text() {
		t.Errorf("second result = %q, want a fresh upstream answer", second.Text())
	}
	if len(cache.entries) != 0 {
		t.Errorf("cache holds %d entries, want 0 for an unknown tool", len(cache.entries))
	}
}

// TestCached_ListBasedNotionSearchIsNotCached covers the same stale-deny shape
// as Alertmanager's getSilences: search results can grow after Notion indexes a
// newly-created page, and absence of that page makes the author gate deny.
func TestCached_ListBasedNotionSearchIsNotCached(t *testing.T) {
	inner := &countingCaller{}
	client := Cached(inner, NewCache())
	args := map[string]any{
		"query":   "new page",
		"filters": map[string]any{"created_by_user_ids": []string{"operator-a"}},
	}
	for i := 0; i < 2; i++ {
		if _, err := client.CallTool(context.Background(), notionSearchTool, args); err != nil {
			t.Fatalf("CallTool %d: %v", i, err)
		}
	}
	if got := inner.count(); got != 2 {
		t.Errorf("notion search called upstream %d times, want 2 (list-shaped search results must stay fresh)", got)
	}
}

// TestCached_EveryReviewedLookupToolIsCacheable keeps the allowlist aligned
// with the complete current lookup surface. A missing entry is fail-safe but
// loses the optimization this cache exists to provide.
func TestCached_EveryReviewedLookupToolIsCacheable(t *testing.T) {
	tools := []string{
		notionFetchTool,
		linearGetIssueTool,
		linearGetProjectTool,
		githubPullRequestReadTool,
		gitlabGetMergeRequestTool,
		gitlabGetProjectTool,
		jiraGetIssueTool,
		"pagerduty_get_incident",
		"pagerduty_secondary_get_incident",
	}
	for _, tool := range tools {
		t.Run(tool, func(t *testing.T) {
			inner := &countingCaller{}
			client := Cached(inner, NewCache())
			args := map[string]any{"id": "resource-a"}
			for i := 0; i < 2; i++ {
				if _, err := client.CallTool(context.Background(), tool, args); err != nil {
					t.Fatalf("CallTool %d: %v", i, err)
				}
			}
			if got := inner.count(); got != 1 {
				t.Errorf("upstream called %d times, want 1", got)
			}
		})
	}
}

// TestCached_KeySeparatesDistinctLookups pins what does and does not count as
// the same lookup. Anything that could change the answer must miss; argument
// ORDER cannot change the answer, so it must hit (json.Marshal sorts map keys).
func TestCached_KeySeparatesDistinctLookups(t *testing.T) {
	tests := []struct {
		name      string
		firstTool string
		firstArgs map[string]any
		nextTool  string
		nextArgs  map[string]any
		wantCalls int
	}{
		{
			name:      "same tool and args hits",
			firstTool: jiraGetIssueTool, firstArgs: map[string]any{"issue_key": "CHANGE-1"},
			nextTool: jiraGetIssueTool, nextArgs: map[string]any{"issue_key": "CHANGE-1"},
			wantCalls: 1,
		},
		{
			name:      "different tool misses",
			firstTool: jiraGetIssueTool, firstArgs: map[string]any{"id": "x"},
			nextTool: linearGetIssueTool, nextArgs: map[string]any{"id": "x"},
			wantCalls: 2,
		},
		{
			name:      "different argument value misses",
			firstTool: notionFetchTool, firstArgs: map[string]any{"id": "page-a"},
			nextTool: notionFetchTool, nextArgs: map[string]any{"id": "page-b"},
			wantCalls: 2,
		},
		{
			name:      "extra argument misses",
			firstTool: githubPullRequestReadTool, firstArgs: map[string]any{"owner": "o", "repo": "r"},
			nextTool: githubPullRequestReadTool, nextArgs: map[string]any{"owner": "o", "repo": "r", "pullNumber": 7.0},
			wantCalls: 2,
		},
		{
			name:      "nested argument difference misses",
			firstTool: linearGetProjectTool, firstArgs: map[string]any{"query": map[string]any{"team": "A"}},
			nextTool: linearGetProjectTool, nextArgs: map[string]any{"query": map[string]any{"team": "B"}},
			wantCalls: 2,
		},
		{
			name:      "same arguments built in a different order hits",
			firstTool: githubPullRequestReadTool, firstArgs: map[string]any{"owner": "o", "repo": "r", "pullNumber": 7.0},
			nextTool: githubPullRequestReadTool, nextArgs: map[string]any{"pullNumber": 7.0, "repo": "r", "owner": "o"},
			wantCalls: 1,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			inner := &countingCaller{}
			cache, _ := testCache()
			client := Cached(inner, cache)
			if _, err := client.CallTool(context.Background(), tc.firstTool, tc.firstArgs); err != nil {
				t.Fatalf("first CallTool: %v", err)
			}
			if _, err := client.CallTool(context.Background(), tc.nextTool, tc.nextArgs); err != nil {
				t.Fatalf("second CallTool: %v", err)
			}
			if got := inner.count(); got != tc.wantCalls {
				t.Errorf("upstream called %d times, want %d", got, tc.wantCalls)
			}
		})
	}
}

// TestCached_EntriesExpire covers the whole point of the TTL: a lookup whose
// answer can change is re-resolved once the window passes, and the caller sees
// the NEW answer rather than the remembered one.
func TestCached_EntriesExpire(t *testing.T) {
	inner := &countingCaller{}
	cache, advance := testCache()
	client := Cached(inner, cache)

	args := map[string]any{"issue_key": "CHANGE-1"}
	if _, err := client.CallTool(context.Background(), jiraGetIssueTool, args); err != nil {
		t.Fatalf("first CallTool: %v", err)
	}
	advance(DefaultCacheTTL - time.Second)
	if _, err := client.CallTool(context.Background(), jiraGetIssueTool, args); err != nil {
		t.Fatalf("in-window CallTool: %v", err)
	}
	if got := inner.count(); got != 1 {
		t.Fatalf("in-window lookup called upstream %d times, want 1", got)
	}

	advance(2 * time.Second)
	result, err := client.CallTool(context.Background(), jiraGetIssueTool, args)
	if err != nil {
		t.Fatalf("post-expiry CallTool: %v", err)
	}
	if got := inner.count(); got != 2 {
		t.Errorf("post-expiry lookup called upstream %d times, want 2", got)
	}
	if result.Text() != "r2" {
		t.Errorf("post-expiry result = %q, want the fresh %q", result.Text(), "r2")
	}
}

// TestCacheTTLFor classifies every reviewed authorization signal by how long
// the value the gate actually consumes can remain valid. Keep this exhaustive:
// adding a tool without choosing a freshness class must fail safe in production
// and fail visibly here.
func TestCacheTTLFor(t *testing.T) {
	tests := []struct {
		name string
		tool string
		args map[string]any
		want time.Duration
	}{
		{name: "Notion page ancestry and owner are mutable", tool: notionFetchTool, args: map[string]any{"id": "page-a"}, want: DefaultCacheTTL},
		{name: "Linear issue team and assignee are mutable", tool: linearGetIssueTool, args: map[string]any{"id": "ISSUE-1"}, want: DefaultCacheTTL},
		{name: "Linear project teams are mutable", tool: linearGetProjectTool, args: map[string]any{"query": "project-a"}, want: DefaultCacheTTL},
		{name: "Jira assignee is mutable", tool: jiraGetIssueTool, args: map[string]any{"issue_key": "CHANGE-1"}, want: DefaultCacheTTL},
		{name: "GitHub author is stable but repository path is reusable", tool: githubPullRequestReadTool, args: map[string]any{"owner": "org", "repo": "repo", "pullNumber": 1, "method": "get"}, want: pathIdentityCacheTTL},
		{name: "GitHub review response is mutable", tool: githubPullRequestReadTool, args: map[string]any{"owner": "org", "repo": "repo", "pullNumber": 1, "method": "get_reviews"}, want: DefaultCacheTTL},
		{name: "GitLab project id is stable but project path is reusable", tool: gitlabGetProjectTool, args: map[string]any{"project_id": "group/project"}, want: pathIdentityCacheTTL},
		{name: "GitLab MR author username is stable but mutable", tool: gitlabGetMergeRequestTool, args: map[string]any{"project_id": "148", "merge_request_iid": "754"}, want: pathIdentityCacheTTL},
		{name: "GitLab MR author by project path is bounded by path reuse", tool: gitlabGetMergeRequestTool, args: map[string]any{"project_id": "group/project", "merge_request_iid": "754"}, want: pathIdentityCacheTTL},
		{name: "GitLab MR source branch can resolve a later MR", tool: gitlabGetMergeRequestTool, args: map[string]any{"project_id": "148", "source_branch": "feature"}, want: DefaultCacheTTL},
		{name: "GitLab source branch wins when both selectors are present", tool: gitlabGetMergeRequestTool, args: map[string]any{"project_id": "148", "merge_request_iid": "754", "source_branch": "feature"}, want: DefaultCacheTTL},
		{name: "PagerDuty incident service is mutable", tool: "pagerduty_get_incident", args: map[string]any{"incident_id": "P123"}, want: DefaultCacheTTL},
		{name: "PagerDuty gov incident service is mutable", tool: "pagerduty_secondary_get_incident", args: map[string]any{"incident_id": "P123"}, want: DefaultCacheTTL},
	}
	reviewed := make(map[string]struct{}, len(tests))
	for _, tc := range tests {
		reviewed[tc.tool] = struct{}{}
		t.Run(tc.name, func(t *testing.T) {
			got, cacheable := cacheTTLFor(tc.tool, tc.args)
			if !cacheable {
				t.Fatal("reviewed tool unexpectedly bypasses cache")
			}
			if got != tc.want {
				t.Errorf("cacheTTLFor() = %s, want %s", got, tc.want)
			}
		})
	}
	if len(reviewed) != len(cacheTTLByTool) {
		t.Fatalf("policy cases cover %d unique tools, allowlist contains %d", len(reviewed), len(cacheTTLByTool))
	}
	for tool := range cacheTTLByTool {
		if _, ok := reviewed[tool]; !ok {
			t.Errorf("allowlisted tool %q has no freshness-policy test case", tool)
		}
	}
	if _, cacheable := cacheTTLFor("future_lookup", map[string]any{"id": "x"}); cacheable {
		t.Fatal("unknown tool unexpectedly cacheable")
	}
}

// TestCached_PerToolTTLOverride proves the selected TTL applies in storage: the
// GitLab project lookup outlives a mutable lookup's default window.
func TestCached_PerToolTTLOverride(t *testing.T) {
	inner := &countingCaller{}
	cache, advance := testCache()
	client := Cached(inner, cache)

	projectArgs := map[string]any{"project_id": "group/project"}
	issueArgs := map[string]any{"issue_key": "CHANGE-1"}
	if _, err := client.CallTool(context.Background(), gitlabGetProjectTool, projectArgs); err != nil {
		t.Fatalf("project CallTool: %v", err)
	}
	if _, err := client.CallTool(context.Background(), jiraGetIssueTool, issueArgs); err != nil {
		t.Fatalf("issue CallTool: %v", err)
	}

	advance(pathIdentityCacheTTL - time.Minute) // past the default TTL, inside the override
	if _, err := client.CallTool(context.Background(), gitlabGetProjectTool, projectArgs); err != nil {
		t.Fatalf("project CallTool after default TTL: %v", err)
	}
	if _, err := client.CallTool(context.Background(), jiraGetIssueTool, issueArgs); err != nil {
		t.Fatalf("issue CallTool after default TTL: %v", err)
	}
	if got := inner.count(); got != 3 {
		t.Errorf("upstream called %d times, want 3 (project still cached, issue re-resolved)", got)
	}

	advance(2 * time.Minute) // now past the override too
	if _, err := client.CallTool(context.Background(), gitlabGetProjectTool, projectArgs); err != nil {
		t.Fatalf("project CallTool after override TTL: %v", err)
	}
	if got := inner.count(); got != 4 {
		t.Errorf("upstream called %d times, want 4 (project re-resolved after its own TTL)", got)
	}
}

// TestCached_FailuresAreNeverCached is the fail-closed protection: a gate
// denies on lookup error, so remembering an error would stretch one blip into
// a TTL of denied legitimate work.
func TestCached_FailuresAreNeverCached(t *testing.T) {
	inner := &countingCaller{err: errors.New("connection refused")}
	cache, _ := testCache()
	client := Cached(inner, cache)

	args := map[string]any{"id": "page-a"}
	if _, err := client.CallTool(context.Background(), notionFetchTool, args); err == nil {
		t.Fatal("expected the upstream error to surface")
	}
	inner.mu.Lock()
	inner.err = nil
	inner.mu.Unlock()

	result, err := client.CallTool(context.Background(), notionFetchTool, args)
	if err != nil {
		t.Fatalf("retry after transient failure: %v", err)
	}
	if result.Text() != "r2" {
		t.Errorf("retry returned %q, want the fresh result once upstream recovered", result.Text())
	}
	if got := inner.count(); got != 2 {
		t.Errorf("upstream called %d times, want 2 (the failure must not have been stored)", got)
	}
}

// TestCache_EvictsWhenFull keeps a pod's memory bounded no matter how many
// distinct resources an agent walks through. Entries are staggered in time so
// "closest to expiring" is unambiguous.
func TestCache_EvictsWhenFull(t *testing.T) {
	cache, advance := testCache()
	for i := 0; i < maxCacheEntries; i++ {
		cache.put(fmt.Sprintf("key-%d", i), newTextResult("v"), DefaultCacheTTL)
		advance(time.Millisecond)
	}
	if got := len(cache.entries); got != maxCacheEntries {
		t.Fatalf("cache holds %d entries, want %d", got, maxCacheEntries)
	}

	cache.put("key-new", newTextResult("v"), DefaultCacheTTL)
	if got := len(cache.entries); got != maxCacheEntries {
		t.Errorf("cache holds %d entries after overflow, want %d", got, maxCacheEntries)
	}
	if _, ok := cache.get("key-0"); ok {
		t.Error("earliest-expiring entry survived; the cache is not bounded")
	}
	if _, ok := cache.get("key-new"); !ok {
		t.Error("newest entry was not stored")
	}
}

// TestCache_EvictsToStayWithinTotalByteBudget proves many individually valid
// entries cannot collectively consume the old entries*per-entry worst case.
// Keys count too because lookup arguments are controlled by agent traffic.
func TestCache_EvictsToStayWithinTotalByteBudget(t *testing.T) {
	cache, advance := testCache()
	payload := strings.Repeat("x", maxCachedResultBytes/2)
	for i := 0; i < maxCacheEntries; i++ {
		cache.put(fmt.Sprintf("key-%03d", i), newTextResult(payload), DefaultCacheTTL)
		advance(time.Millisecond)
	}

	if got := len(cache.entries); got >= maxCacheEntries {
		t.Fatalf("cache retained %d large entries, want byte-budget eviction before the %d-entry cap", got, maxCacheEntries)
	}
	if _, ok := cache.get("key-000"); ok {
		t.Error("earliest-expiring entry survived total-byte-budget eviction")
	}

	total := 0
	for key, entry := range cache.entries {
		total += len(key) + resultSize(entry.result)
	}
	if total > maxCacheBytes {
		t.Errorf("cache retains %d variable bytes, exceeds %d-byte budget", total, maxCacheBytes)
	}
	if cache.totalBytes != total {
		t.Errorf("accounted bytes = %d, want measured %d", cache.totalBytes, total)
	}
}

func TestCache_ReplacingLiveKeyDoesNotDoubleCountBytes(t *testing.T) {
	cache := NewCache()
	const key = "same-key"
	cache.put(key, newTextResult("first"), DefaultCacheTTL)
	cache.put(key, newTextResult("a larger replacement"), DefaultCacheTTL)

	entry, ok := cache.entries[key]
	if !ok {
		t.Fatal("replacement was not stored")
	}
	want := len(key) + resultSize(entry.result)
	if cache.totalBytes != want {
		t.Errorf("accounted bytes = %d, want replacement-only size %d", cache.totalBytes, want)
	}
}

func TestCache_OversizedKeyIsNotStored(t *testing.T) {
	cache := NewCache()
	cache.put(strings.Repeat("k", maxCacheBytes+1), newTextResult("small result"), DefaultCacheTTL)
	if len(cache.entries) != 0 || cache.totalBytes != 0 {
		t.Errorf("oversized key was retained: entries=%d bytes=%d", len(cache.entries), cache.totalBytes)
	}
}

// TestCache_ExpiredEntriesAreSwept keeps a burst of one-off lookups from
// evicting live entries after they've all gone stale anyway.
func TestCache_ExpiredEntriesAreSwept(t *testing.T) {
	cache, advance := testCache()
	for i := 0; i < maxCacheEntries; i++ {
		cache.put(fmt.Sprintf("key-%d", i), newTextResult("v"), DefaultCacheTTL)
	}
	advance(DefaultCacheTTL + time.Second)
	cache.put("key-new", newTextResult("v"), DefaultCacheTTL)
	if got := len(cache.entries); got != 1 {
		t.Errorf("cache holds %d entries, want 1 (the expired ones should be gone)", got)
	}
	entry := cache.entries["key-new"]
	wantBytes := len("key-new") + resultSize(entry.result)
	if cache.totalBytes != wantBytes {
		t.Errorf("accounted bytes after sweep = %d, want %d", cache.totalBytes, wantBytes)
	}
}

// TestCache_OversizedResultIsNotStored keeps one huge page fetch from
// occupying the whole budget of a container that requests 32Mi.
func TestCache_OversizedResultIsNotStored(t *testing.T) {
	inner := &countingCaller{text: func(int) string { return strings.Repeat("x", maxCachedResultBytes+1) }}
	cache, _ := testCache()
	client := Cached(inner, cache)

	args := map[string]any{"id": "huge-page"}
	for i := 0; i < 2; i++ {
		if _, err := client.CallTool(context.Background(), notionFetchTool, args); err != nil {
			t.Fatalf("CallTool %d: %v", i, err)
		}
	}
	if got := inner.count(); got != 2 {
		t.Errorf("upstream called %d times, want 2 (an oversized result must not be stored)", got)
	}
	if len(cache.entries) != 0 {
		t.Errorf("cache holds %d entries, want 0", len(cache.entries))
	}
}

// TestCache_ManyEmptyContentBlocksAreNotFree proves the byte budget includes
// the retained slice elements, not only the strings inside them. The current
// lookup tools return one text block, but the cache must bound valid MCP result
// shapes rather than relying on that convention.
func TestCache_ManyEmptyContentBlocksAreNotFree(t *testing.T) {
	result := &CallToolResult{Content: make([]struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}, 5000)}
	cache := NewCache()
	cache.put("many-empty-blocks", result, DefaultCacheTTL)
	if len(cache.entries) != 0 {
		t.Errorf("cache holds %d entries, want 0 for a result whose block metadata exceeds the per-result cap", len(cache.entries))
	}
}

// TestCached_NilCacheIsPassthrough is the off switch: no cache means no
// decorator in the path at all, not a decorator that never hits.
func TestCached_NilCacheIsPassthrough(t *testing.T) {
	inner := &countingCaller{}
	if got := Cached(inner, nil); got != ToolCaller(inner) {
		t.Fatalf("Cached with a nil cache returned %T, want the client unchanged", got)
	}

	client := Cached(inner, nil)
	args := map[string]any{"id": "page-a"}
	for i := 0; i < 3; i++ {
		if _, err := client.CallTool(context.Background(), notionFetchTool, args); err != nil {
			t.Fatalf("CallTool %d: %v", i, err)
		}
	}
	if got := inner.count(); got != 3 {
		t.Errorf("upstream called %d times, want 3 (caching disabled)", got)
	}
}

func TestUncached_StripsTheCacheLayer(t *testing.T) {
	inner := &countingCaller{}
	cache, _ := testCache()

	if got := Uncached(Cached(inner, cache)); got != ToolCaller(inner) {
		t.Errorf("Uncached returned %T, want the wrapped client", got)
	}
	if got := Uncached(inner); got != ToolCaller(inner) {
		t.Errorf("Uncached of an unwrapped client returned %T, want it unchanged", got)
	}
}

// TestSilenceCreatedBy_IsNeverCached is the reason Uncached exists: every
// getSilences call shares one cache key (empty arguments) and the result is a
// list that grows, so a cached copy would report a just-created silence as
// missing -- which the gate turns into a deny.
func TestSilenceCreatedBy_IsNeverCached(t *testing.T) {
	const created = `[{"id":"sil-1","createdBy":"vicegerent-personal"},{"id":"sil-2","createdBy":"vicegerent-personal"}]`
	inner := &countingCaller{text: func(n int) string {
		if n == 1 {
			return `[{"id":"sil-1","createdBy":"vicegerent-personal"}]` // sil-2 not created yet
		}
		return created
	}}
	cache, _ := testCache()
	client := Cached(inner, cache)

	if _, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", client, "sil-1"); err != nil {
		t.Fatalf("first lookup: %v", err)
	}
	owner, err := SilenceCreatedBy(context.Background(), "alertmanager_getSilences", client, "sil-2")
	if err != nil {
		t.Fatalf("lookup of a silence created after the first call: %v", err)
	}
	if owner != "vicegerent-personal" {
		t.Errorf("owner = %q, want %q", owner, "vicegerent-personal")
	}
	if got := inner.count(); got != 2 {
		t.Errorf("upstream called %d times, want 2 (getSilences must bypass the cache)", got)
	}
}

// TestCached_CallerCannotCorruptTheCache: a caller that mutates the result it
// was handed must not change what the next caller sees.
func TestCached_CallerCannotCorruptTheCache(t *testing.T) {
	inner := &countingCaller{}
	cache, _ := testCache()
	client := Cached(inner, cache)

	args := map[string]any{"id": "page-a"}
	first, err := client.CallTool(context.Background(), notionFetchTool, args)
	if err != nil {
		t.Fatalf("first CallTool: %v", err)
	}
	first.Content[0].Text = "tampered"

	second, err := client.CallTool(context.Background(), notionFetchTool, args)
	if err != nil {
		t.Fatalf("second CallTool: %v", err)
	}
	if second.Text() != "r1" {
		t.Errorf("cached result = %q, want the unmodified %q", second.Text(), "r1")
	}
}

// TestCached_Concurrent is a -race check: gates run on independent gRPC
// streams and share one cache.
func TestCached_Concurrent(t *testing.T) {
	inner := &countingCaller{}
	client := Cached(inner, NewCache())

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			args := map[string]any{"id": fmt.Sprintf("page-%d", i%5)}
			if _, err := client.CallTool(context.Background(), notionFetchTool, args); err != nil {
				t.Errorf("CallTool: %v", err)
			}
		}(i)
	}
	wg.Wait()
}

// TestCached_OverRealClient is the end-to-end proof over the actual MCP client
// and a real HTTP server: a run of calls naming the same project by path costs
// ONE tools/call, and the caller still gets the right canonical id every time.
func TestCached_OverRealClient(t *testing.T) {
	var toolCalls int64
	srv := newTestServer(t, func(name string, args map[string]any) (int, string) {
		atomic.AddInt64(&toolCalls, 1)
		if name != gitlabGetProjectTool {
			t.Errorf("unexpected tool name %q", name)
		}
		return http.StatusOK, `{"id":148,"path_with_namespace":"jchristensen/vicegerent-agents"}`
	})
	defer srv.Close()

	client := Cached(New(srv.URL, nil), NewCache())
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for i := 0; i < 4; i++ {
		id, err := CanonicalProjectID(ctx, client, "jchristensen/vicegerent-agents")
		if err != nil {
			t.Fatalf("CanonicalProjectID %d: %v", i, err)
		}
		if id != "148" {
			t.Fatalf("CanonicalProjectID %d = %q, want %q", i, id, "148")
		}
	}
	if got := atomic.LoadInt64(&toolCalls); got != 1 {
		t.Errorf("server saw %d tools/call requests, want 1", got)
	}
}
