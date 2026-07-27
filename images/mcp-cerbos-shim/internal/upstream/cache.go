package upstream

import (
	"context"
	"encoding/json"
	"sync"
	"time"
)

// This file makes reviewed lookups the shim issues as an MCP client cacheable
// through one decorator. internal/upstream is the only place the shim acts as
// a client (see client.go's package doc), and every lookup in it funnels
// through ToolCaller.CallTool. The decorator covers that whole seam, while
// cacheTTLByTool keeps the actual freshness decision explicit and fail-safe:
// a future gate bypasses the cache until its result semantics are reviewed.
//
// It replaces a gate-specific memoization (the GitLab project-canonicalization
// cache, which stored the parsed numeric id keyed on the raw spelling the
// caller sent). Caching the raw tool RESULT instead of each gate's parsed
// value is what generalizes: the key is the same thing every gate varies (the
// tool plus its arguments), so two reviewed gates that happen to make the same
// call -- the Notion ancestry gate and the Notion page-author gate both
// fetching the same page id during one CheckRequest -- share one round trip
// instead of each maintaining its own private map.
//
// SCOPE OF THE KEY: (tool, arguments) is a complete key ONLY because every
// lookup runs with the same identity -- the shim's own vMCP credentials, over
// one route (DefaultVMCPURL), on behalf of a single fixed principal. If the
// shim ever performs lookups as more than one identity, that identity has to
// become part of the key, or one caller would be served another's view of a
// resource. Same requirement when this moves to Redis and the cache stops
// being per-pod.

// DefaultCacheTTL bounds reuse of a lookup whose answer can legitimately
// change while the shim is running: an issue's assignee, a page's parent
// chain, a project's teams. These feed authorization, so the window is
// deliberately short -- long enough to collapse the burst of identical lookups
// one agent task produces (the same issue re-resolved by three consecutive
// comment calls), short enough that a human who reassigns a ticket or moves a
// page isn't fighting a stale allow -- or, just as bad, a stale DENY -- for
// the rest of their afternoon.
const DefaultCacheTTL = time.Minute

// pathIdentityCacheTTL applies when the gate consumes a stable identity signal
// but the cache key or compared identity uses a reusable name. GitLab project
// canonicalization reads only the permanent numeric project id, while GitHub
// and GitLab author gates compare usernames. Project paths, repository paths,
// and usernames can all be renamed and eventually claimed by another resource
// or user. Ten minutes keeps those lookups hot without pretending a name is a
// permanent identity.
const pathIdentityCacheTTL = 10 * time.Minute

// cacheTTLByTool is the explicit allowlist of lookup calls that are safe to
// cache for their current authorization consumer. Unknown tools bypass the
// cache: this is authorization data, so a new gate must make a deliberate
// freshness decision rather than inheriting stale behavior merely because it
// calls through ToolCaller. cacheTTLFor refines argument-sensitive entries.
//
// The PagerDuty tool names come from server.go's
// pagerdutyManageIncidentsTools. Keeping them here is intentionally fail-safe:
// adding or renaming a backend without updating this list only loses a cache
// optimization; it cannot accidentally cache a lookup whose semantics have
// not been reviewed. List-shaped results that can grow are omitted: Notion
// search can lag a newly-created page, and Alertmanager getSilences can miss a
// newly-created silence. Both absences become stale denies in their gates (see
// PageAuthoredByOperator and SilenceCreatedBy).
var cacheTTLByTool = map[string]time.Duration{
	notionFetchTool:              DefaultCacheTTL,
	linearGetIssueTool:           DefaultCacheTTL,
	linearGetProjectTool:         DefaultCacheTTL,
	githubPullRequestReadTool:    pathIdentityCacheTTL,
	gitlabGetMergeRequestTool:    pathIdentityCacheTTL,
	gitlabGetProjectTool:         pathIdentityCacheTTL,
	jiraGetIssueTool:             DefaultCacheTTL,
	"pagerduty_get_incident":     DefaultCacheTTL,
	"pagerduty_gov_get_incident": DefaultCacheTTL,
}

// cacheTTLFor returns the reviewed TTL for one concrete call. Most tools have
// one freshness class. GitHub pull_request_read and GitLab get_merge_request
// are argument-sensitive: only the exact result shape the authorization gate
// consumes earns the path-identity TTL. Other GitHub methods return mutable
// review/file/status data, and a GitLab source branch can later select a
// different MR, so those forms use the one-minute default.
//
// TTLs are based on the fields the current authorization lookup consumes, not
// every incidental mutable field in the upstream response. A future consumer
// of an already-listed tool must review this policy before reading more fields.
func cacheTTLFor(tool string, arguments map[string]any) (time.Duration, bool) {
	ttl, cacheable := cacheTTLByTool[tool]
	if !cacheable {
		return 0, false
	}
	if tool == githubPullRequestReadTool && stringArgument(arguments, "method") != "get" {
		return DefaultCacheTTL, true
	}
	if tool == gitlabGetMergeRequestTool && stringArgument(arguments, "source_branch") != "" {
		return DefaultCacheTTL, true
	}
	return ttl, true
}

func stringArgument(arguments map[string]any, key string) string {
	value, _ := arguments[key].(string)
	return value
}

// maxCacheEntries, maxCachedResultBytes, and maxCacheBytes bound what one pod
// can retain. Entries here are whole tool results -- a fetched Notion page's
// markdown, a project object -- and keys come from agent traffic. The
// per-result cap prevents one response from monopolizing the cache; the total
// budget counts both result text and serialized keys so individually valid
// entries cannot multiply into the old ~16Mi worst case. Fixed map/string
// overhead remains bounded by maxCacheEntries.
const (
	maxCacheEntries      = 128
	maxCachedResultBytes = 128 << 10
	maxCacheBytes        = 4 << 20
	// Two Go string headers per CallToolResult.Content element. This is exact
	// on the 64-bit production target and conservative on 32-bit builds.
	cacheContentBlockOverhead = 32
)

// Cache is an in-memory, per-process store of successful tools/call results.
// Nothing persists across a restart and replicas do not share one, which is
// the accepted trade for now: restarting the shim is the way to clear it, and
// a second replica simply pays its own first lookup. Safe for concurrent use.
type Cache struct {
	mu sync.Mutex
	// now is time.Now in production; tests replace it to drive expiry
	// without sleeping.
	now        func() time.Time
	entries    map[string]cacheEntry
	totalBytes int
}

type cacheEntry struct {
	result    *CallToolResult
	expiresAt time.Time
	size      int
}

// NewCache returns an empty cache ready for Cached to wrap a client with.
func NewCache() *Cache {
	return &Cache{now: time.Now, entries: map[string]cacheEntry{}}
}

// Cached wraps inner so identical lookups within the TTL are served from
// cache. A nil cache returns inner unchanged -- that is the off switch, so
// disabling caching costs nothing at the call sites and leaves no decorator in
// the path (see main.go's cacheUpstreamLookups).
func Cached(inner ToolCaller, cache *Cache) ToolCaller {
	if inner == nil || cache == nil {
		return inner
	}
	return &cachingCaller{inner: inner, cache: cache}
}

// Uncached strips a Cached wrapper, returning the client underneath. For the
// rare lookup that must never be served a remembered answer; anything else
// stays cached. A caller that was never wrapped is returned as-is.
func Uncached(caller ToolCaller) ToolCaller {
	if c, ok := caller.(*cachingCaller); ok {
		return c.inner
	}
	return caller
}

type cachingCaller struct {
	inner ToolCaller
	cache *Cache
}

// CallTool serves a cached result when one is live, otherwise calls through
// and remembers the answer.
//
// Failures are NEVER cached. Every gate fails closed on a lookup error, so a
// remembered failure would stretch one transient upstream blip into a
// TTL-long denial of legitimate work -- the outage shape this cache exists to
// prevent, not cause. A tool that reports its own error surfaces as an error
// from the inner client too (see Client.CallTool), so it is not cached either.
//
// There is no single-flight: two concurrent identical lookups both miss and
// both call upstream, and the later result wins. Gates resolve sequentially
// within a CheckRequest, so the duplicate window is small, and collapsing it
// would mean holding a lock across a network call -- the thundering-herd fix
// belongs with the shared cache (Redis), not here.
func (c *cachingCaller) CallTool(ctx context.Context, tool string, arguments map[string]any) (*CallToolResult, error) {
	ttl, cacheable := cacheTTLFor(tool, arguments)
	if !cacheable {
		return c.inner.CallTool(ctx, tool, arguments)
	}
	key, ok := cacheKey(tool, arguments)
	if !ok {
		return c.inner.CallTool(ctx, tool, arguments)
	}
	if result, hit := c.cache.get(key); hit {
		return result, nil
	}
	result, err := c.inner.CallTool(ctx, tool, arguments)
	if err != nil {
		return nil, err
	}
	c.cache.put(key, result, ttl)
	return result, nil
}

// cacheKey identifies one lookup. json.Marshal sorts map keys, so two calls
// passing the same arguments in a different order share a key. An argument
// that cannot be marshaled (nothing builds one today) yields no key at all,
// so the call goes through uncached rather than colliding with another's.
// The NUL separator can't appear in a tool name or in JSON, so no
// tool/argument pair can be spelled two ways.
func cacheKey(tool string, arguments map[string]any) (string, bool) {
	args, err := json.Marshal(arguments)
	if err != nil {
		return "", false
	}
	return tool + "\x00" + string(args), true
}

// get returns a live entry's result, or false if the key is absent or expired.
func (c *Cache) get(key string) (*CallToolResult, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[key]
	if !ok {
		return nil, false
	}
	if !c.now().Before(e.expiresAt) {
		c.deleteLocked(key)
		return nil, false
	}
	return cloneResult(e.result), true
}

// put stores a successful result under the explicit TTL its call earns.
// Callers must not put failures (see cachingCaller.CallTool).
func (c *Cache) put(key string, result *CallToolResult, ttl time.Duration) {
	if result == nil {
		return
	}
	resultBytes := resultSize(result)
	entryBytes := len(key) + resultBytes
	if resultBytes > maxCachedResultBytes || entryBytes > maxCacheBytes {
		return
	}
	if ttl <= 0 {
		return
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	for k, e := range c.entries {
		if !now.Before(e.expiresAt) {
			c.deleteLocked(k)
		}
	}
	// Remove a prior value before enforcing capacity so replacement does not
	// count its old bytes or evict an unrelated entry unnecessarily.
	c.deleteLocked(key)
	// Drop entries closest to expiry until both independent bounds have room.
	// Refusing to store instead would freeze the cache on whatever filled it
	// first, which is the opposite of what a run of lookups needs.
	for len(c.entries) >= maxCacheEntries || c.totalBytes+entryBytes > maxCacheBytes {
		if !c.evictNextLocked() {
			return
		}
	}
	c.entries[key] = cacheEntry{
		result:    cloneResult(result),
		expiresAt: now.Add(ttl),
		size:      entryBytes,
	}
	c.totalBytes += entryBytes
}

// deleteLocked removes one entry and its accounted variable bytes. c.mu must
// be held by the caller.
func (c *Cache) deleteLocked(key string) {
	if entry, ok := c.entries[key]; ok {
		delete(c.entries, key)
		c.totalBytes -= entry.size
	}
}

// evictNextLocked removes the live entry closest to expiry. c.mu must be held.
func (c *Cache) evictNextLocked() bool {
	var oldest string
	var oldestAt time.Time
	for key, entry := range c.entries {
		if oldest == "" || entry.expiresAt.Before(oldestAt) {
			oldest, oldestAt = key, entry.expiresAt
		}
	}
	if oldest == "" {
		return false
	}
	c.deleteLocked(oldest)
	return true
}

// cloneResult copies a result so the cache and its callers never share mutable
// state. Nothing mutates a lookup result today; a cache that hands out its
// only copy is a landmine for the first caller that does.
func cloneResult(r *CallToolResult) *CallToolResult {
	if r == nil {
		return nil
	}
	cp := *r
	cp.Content = append(r.Content[:0:0], r.Content...)
	return &cp
}

// resultSize approximates the variable memory retained by a result: the text
// bytes plus the Content slice's two string headers per block. The result
// struct itself is fixed overhead and bounded by maxCacheEntries.
func resultSize(r *CallToolResult) int {
	n := len(r.Content) * cacheContentBlockOverhead
	for _, block := range r.Content {
		n += len(block.Type) + len(block.Text)
	}
	return n
}
