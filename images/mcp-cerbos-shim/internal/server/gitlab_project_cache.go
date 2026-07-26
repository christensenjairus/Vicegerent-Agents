package server

import (
	"sync"
	"time"
)

// projectCanonicalTTL bounds how long a resolved GitLab project id is trusted.
// A project's numeric id never changes, but its PATH can be reassigned (rename,
// or a move between groups), and after a rename the old path can be claimed by
// a different project. So this cache is keyed on the raw spelling the caller
// sent, and entries must expire -- an unbounded cache would let a stale
// path->id mapping outlive the rename and resolve an allowlist check against
// the WRONG project. Ten minutes keeps the common case free while bounding
// that window to something shorter than any realistic rename-then-reclaim.
const projectCanonicalTTL = 10 * time.Minute

// projectCanonicalCache memoizes non-numeric GitLab project_id -> numeric id
// resolutions so a burst of calls naming a project by path costs one lookup
// rather than one per call.
//
// Only SUCCESSES are cached. A failed resolution is never stored: failures
// fail the gate closed, and caching them would extend a transient upstream
// blip (or a timeout) into TTL-long denial of legitimate work -- exactly the
// outage shape this whole change set exists to remove.
type projectCanonicalCache struct {
	mu  sync.Mutex
	ttl time.Duration
	now func() time.Time
	m   map[string]projectCanonicalEntry
}

type projectCanonicalEntry struct {
	canonical string
	expiresAt time.Time
}

func newProjectCanonicalCache(ttl time.Duration) *projectCanonicalCache {
	return &projectCanonicalCache{
		ttl: ttl,
		now: time.Now,
		m:   map[string]projectCanonicalEntry{},
	}
}

// get returns the cached canonical id for raw, if present and unexpired.
func (c *projectCanonicalCache) get(raw string) (string, bool) {
	if c == nil {
		return "", false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.m[raw]
	if !ok {
		return "", false
	}
	if !c.now().Before(e.expiresAt) {
		delete(c.m, raw)
		return "", false
	}
	return e.canonical, true
}

// put stores a successful resolution. Callers must not put failures.
func (c *projectCanonicalCache) put(raw, canonical string) {
	if c == nil || canonical == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	// Opportunistic sweep: this map is bounded by the number of distinct
	// spellings an agent uses, which is tiny, but expired entries shouldn't
	// accumulate for a long-lived process either.
	now := c.now()
	for k, e := range c.m {
		if !now.Before(e.expiresAt) {
			delete(c.m, k)
		}
	}
	c.m[raw] = projectCanonicalEntry{canonical: canonical, expiresAt: now.Add(c.ttl)}
}
