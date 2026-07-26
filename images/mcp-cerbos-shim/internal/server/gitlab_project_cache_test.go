package server

import (
	"testing"
	"time"
)

func TestProjectCanonicalCacheHitAndMiss(t *testing.T) {
	c := newProjectCanonicalCache(time.Minute)

	if _, ok := c.get("group/project"); ok {
		t.Fatal("empty cache reported a hit")
	}

	c.put("group/project", "148")
	got, ok := c.get("group/project")
	if !ok || got != "148" {
		t.Fatalf("get after put = (%q, %v), want (\"148\", true)", got, ok)
	}

	// Keyed on the RAW spelling: a different spelling of the same project is
	// a separate entry, because that's the string the allowlist check sees.
	if _, ok := c.get("GROUP/PROJECT"); ok {
		t.Error("cache should key on the raw spelling, not a normalized form")
	}
}

func TestProjectCanonicalCacheExpires(t *testing.T) {
	c := newProjectCanonicalCache(time.Minute)
	now := time.Now()
	c.now = func() time.Time { return now }

	c.put("group/project", "148")
	if _, ok := c.get("group/project"); !ok {
		t.Fatal("entry missing immediately after put")
	}

	// A project PATH can be reassigned (rename, or a move between groups) and
	// the old path reclaimed by a different project, so a stale mapping must
	// not outlive the TTL — it would resolve an allowlist check against the
	// wrong project.
	now = now.Add(time.Minute + time.Second)
	if _, ok := c.get("group/project"); ok {
		t.Error("expired entry was still served")
	}
}

func TestProjectCanonicalCacheNeverStoresFailures(t *testing.T) {
	c := newProjectCanonicalCache(time.Minute)
	// The gate calls put only on success, but guard the invariant anyway: an
	// empty canonical id must never be cached, or a transient upstream blip
	// would become TTL-long denial of legitimate work.
	c.put("group/project", "")
	if _, ok := c.get("group/project"); ok {
		t.Error("empty canonical id was cached")
	}
}

func TestProjectCanonicalCacheNilSafe(t *testing.T) {
	// The gate holds a nil cache when the canonicalizer option isn't set, so
	// both methods must tolerate it rather than panicking inside a gate.
	var c *projectCanonicalCache
	if _, ok := c.get("group/project"); ok {
		t.Error("nil cache reported a hit")
	}
	c.put("group/project", "148") // must not panic
}

// The whole point of the cache is that a repeated non-numeric spelling stops
// costing a network lookup, so assert the miss->hit transition directly.
func TestProjectCanonicalCacheServesRepeatLookups(t *testing.T) {
	c := newProjectCanonicalCache(projectCanonicalTTL)

	const raw = "jchristensen/vicegerent-agents"
	if _, ok := c.get(raw); ok {
		t.Fatal("unexpected hit before first resolution")
	}
	c.put(raw, "148")

	for i := 0; i < 5; i++ {
		got, ok := c.get(raw)
		if !ok || got != "148" {
			t.Fatalf("repeat lookup %d = (%q, %v), want (\"148\", true)", i, got, ok)
		}
	}
}
