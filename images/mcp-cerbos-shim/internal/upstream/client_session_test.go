package upstream

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// These tests cover the wire-level behaviour of the session cache against a
// real httptest server, because that is the layer where the production bug
// lived: every gate's decision logic was correct, and every existing test
// passed, while the deployed path silently paid two round trips per lookup and
// blew its deadline. The load-bearing assertions here are COUNTS of
// initialize requests, which no other observable exposes.

// mcpTestServer is a minimal streamable-HTTP MCP server: it counts
// initialize/tools-call requests and can be told to reject session ids.
type mcpTestServer struct {
	mu sync.Mutex

	initializeCount int
	toolCallCount   int

	// sessionsRejectedUntil, when > 0, makes the server answer 404 to the
	// first N session-bearing requests, simulating a session the server no
	// longer knows (restart / idle eviction).
	sessionsRejectedUntil int

	// perCallDelay is added to every response, for the deadline test.
	perCallDelay time.Duration

	// validSession is the id handed out by the most recent initialize.
	validSession string
}

func (s *mcpTestServer) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, r.ContentLength)
		if r.ContentLength > 0 {
			if _, err := r.Body.Read(body); err != nil && err.Error() != "EOF" {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
		}
		payload := string(body)

		s.mu.Lock()
		delay := s.perCallDelay
		s.mu.Unlock()
		if delay > 0 {
			time.Sleep(delay)
		}

		// notifications/initialized carries no id and expects 202.
		if strings.Contains(payload, "notifications/initialized") {
			w.WriteHeader(http.StatusAccepted)
			return
		}

		if strings.Contains(payload, `"method":"initialize"`) {
			s.mu.Lock()
			s.initializeCount++
			s.validSession = fmt.Sprintf("session-%d", s.initializeCount)
			sess := s.validSession
			s.mu.Unlock()

			w.Header().Set("Mcp-Session-Id", sess)
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}`)
			return
		}

		// tools/call: validate the session id.
		got := r.Header.Get("Mcp-Session-Id")
		s.mu.Lock()
		reject := s.sessionsRejectedUntil > 0
		if reject {
			s.sessionsRejectedUntil--
		}
		valid := s.validSession
		s.mu.Unlock()

		if reject || got != valid {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"error":"unknown session"}`)
			return
		}

		s.mu.Lock()
		s.toolCallCount++
		s.mu.Unlock()

		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"id\":148}"}],"isError":false}}`)
	}
}

func (s *mcpTestServer) counts() (initializes, toolCalls int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.initializeCount, s.toolCallCount
}

// TestCallToolReusesSessionAcrossLookups is the regression test for the
// outage: N sequential lookups must cost exactly ONE handshake. Before the
// session cache this was N handshakes, which is what pushed a single gated
// call to 4997ms against a 5000ms deadline.
func TestCallToolReusesSessionAcrossLookups(t *testing.T) {
	srv := &mcpTestServer{}
	ts := httptest.NewServer(srv.handler())
	defer ts.Close()

	c := New(ts.URL, nil)

	const lookups = 5
	for i := 0; i < lookups; i++ {
		if _, err := c.CallTool(context.Background(), "gitlab_get_project", map[string]any{"project_id": "148"}); err != nil {
			t.Fatalf("lookup %d: unexpected error: %v", i, err)
		}
	}

	initializes, toolCalls := srv.counts()
	if initializes != 1 {
		t.Errorf("initialize count = %d, want 1 (session must be reused; %d handshakes means the cache is not working)", initializes, lookups)
	}
	if toolCalls != lookups {
		t.Errorf("tools/call count = %d, want %d", toolCalls, lookups)
	}
	if got := c.handshakeCount(); got != 1 {
		t.Errorf("client handshakeCount = %d, want 1", got)
	}
}

// TestCallToolRetriesOnceOnRejectedSession covers the failure mode the cache
// introduces: a cached session the server has forgotten. The call must
// transparently re-handshake and succeed, not fail the gate closed.
func TestCallToolRetriesOnceOnRejectedSession(t *testing.T) {
	srv := &mcpTestServer{}
	ts := httptest.NewServer(srv.handler())
	defer ts.Close()

	c := New(ts.URL, nil)

	// Prime a cached session.
	if _, err := c.CallTool(context.Background(), "gitlab_get_project", map[string]any{"project_id": "148"}); err != nil {
		t.Fatalf("priming call: %v", err)
	}

	// Server now forgets exactly one session-bearing request.
	srv.mu.Lock()
	srv.sessionsRejectedUntil = 1
	srv.mu.Unlock()

	if _, err := c.CallTool(context.Background(), "gitlab_get_project", map[string]any{"project_id": "148"}); err != nil {
		t.Fatalf("call after session rejection should have recovered, got: %v", err)
	}

	initializes, _ := srv.counts()
	if initializes != 2 {
		t.Errorf("initialize count = %d, want 2 (one priming handshake + one re-handshake after rejection)", initializes)
	}
}

func TestInvalidateSessionDoesNotClearReplacement(t *testing.T) {
	c := &Client{sessionID: "replacement"}

	c.invalidateSession("rejected")

	if c.sessionID != "replacement" {
		t.Fatalf("sessionID = %q, want replacement session preserved", c.sessionID)
	}
}

// TestCallToolDoesNotRetryForever guards the retry bound: a server that
// rejects every session must produce an error rather than an unbounded loop
// inside a gate holding an agent's call open.
func TestCallToolDoesNotRetryForever(t *testing.T) {
	srv := &mcpTestServer{sessionsRejectedUntil: 1 << 30}
	ts := httptest.NewServer(srv.handler())
	defer ts.Close()

	c := New(ts.URL, nil)

	_, err := c.CallTool(context.Background(), "gitlab_get_project", map[string]any{"project_id": "148"})
	if err == nil {
		t.Fatal("expected an error when every session is rejected")
	}
	initializes, _ := srv.counts()
	if initializes > 2 {
		t.Errorf("initialize count = %d, want at most 2 (exactly one retry)", initializes)
	}
}

// TestCallToolRespectsDeadline is the test class that was missing entirely and
// is why the 5s timeout bug shipped: every other test injects an in-process
// fake that returns instantly, so no test asserted what happens when a lookup
// outruns its budget. A context deadline must surface as an error the gate can
// fail closed on.
func TestCallToolRespectsDeadline(t *testing.T) {
	srv := &mcpTestServer{perCallDelay: 200 * time.Millisecond}
	ts := httptest.NewServer(srv.handler())
	defer ts.Close()

	c := New(ts.URL, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_, err := c.CallTool(ctx, "gitlab_get_project", map[string]any{"project_id": "148"})
	if err == nil {
		t.Fatal("expected a deadline error when the lookup outruns its budget")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("error = %v, want it to wrap context.DeadlineExceeded", err)
	}
}

// TestConcurrentFirstLookupsHandshakeOnce covers the thundering herd on shim
// startup: a burst of concurrent first-lookups must collapse to ONE
// initialize, otherwise a restart reproduces the original latency spike.
func TestConcurrentFirstLookupsHandshakeOnce(t *testing.T) {
	srv := &mcpTestServer{}
	ts := httptest.NewServer(srv.handler())
	defer ts.Close()

	c := New(ts.URL, nil)

	var wg sync.WaitGroup
	const callers = 8
	errs := make([]error, callers)
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, errs[i] = c.CallTool(context.Background(), "gitlab_get_project", map[string]any{"project_id": "148"})
		}(i)
	}
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("concurrent caller %d: %v", i, err)
		}
	}

	if initializes, _ := srv.counts(); initializes != 1 {
		t.Errorf("initialize count = %d, want 1 (concurrent first lookups must share one handshake)", initializes)
	}
}
