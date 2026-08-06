// Package upstream makes the shim itself an MCP client, calling back into
// vMCP (via agentgateway, in-cluster HTTP) to resolve state the Cerbos
// CheckRequest path cannot see from the request being evaluated alone --
// e.g. a Notion page's ancestry, which isn't in the notion_notion-update-page
// call's own args. See internal/eval/eval.go's package doc for why this
// can't live in a CEL helper: CEL programs are synchronous pure functions
// with no I/O, and this needs a real network round trip.
//
// RECURSION-SAFETY NOTE (read before mapping notion_notion-fetch in Cerbos):
// every lookup this package makes is itself a tools/call that re-enters the
// shim's own CheckRequest gate exactly once before agentgateway forwards it to
// vMCP. The dedicated route deliberately has no response hook for tools/call.
//
// CheckRequest leg: today notion_notion-fetch is completely unmapped in
// mapping.yaml (falls through to defaultAction: allow), so that re-entry
// passes straight through Cerbos and there is no loop. If a future Cerbos
// policy or mapping.yaml entry ever adds a deny rule (or any check requiring
// another lookup) for notion_notion-fetch, this call could start getting
// denied, silently breaking every ancestry check that depends on it (they
// fail closed -- see PageIsUnderAnyAncestor -- so the failure mode is "always
// deny", not silent-allow, but it will look like an unrelated regression).
// Keep notion_notion-fetch unmapped, or if it ever needs a policy, make sure
// this package's calls are exempted or the ancestry check is redesigned.
//
// CheckResponse leg: the prompt-injection gate (server.checkPromptInjection)
// runs on the RESULT of this package's own lookups too. A lookup that fetches
// attacker-controlled content (e.g. a Notion page carrying a prompt injection,
// whose author this package is resolving) would otherwise be DENIED by the
// shim's own response gate -- the lookup errors, the ownership gate fails
// closed, and the agent's original call is denied with a misleading reason.
// To break that loop, this package targets a dedicated route whose required
// methods run CheckRequest but not CheckResponse. Two independent locks keep agents off that
// route: the :81 listener is restricted to the shim pod by a CiliumNetworkPolicy
// (network/port), and CheckRequest denies any caller on the vmcp-internal
// backend that doesn't present the shim's secret self-token in the
// SelfHeaderName header (app/backend). The token lives in a Secret only the
// shim pod can read, so agents cannot forge it. See server.go's
// isInternalBackend / isSelfRequest and README "Re-Entrant Lookup Path".
package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
)

// errSessionRejected marks a non-200 that means "this Mcp-Session-Id is no
// longer valid" rather than "the upstream is broken". Only these are worth
// retrying on a fresh handshake; anything else is surfaced as-is so a real
// upstream failure still fails the gate closed instead of being retried into
// a doubled latency budget.
var errSessionRejected = errors.New("upstream rejected the MCP session")

// SelfHeaderName is the HTTP header the shim's own MCP client sets (to its
// secret self-token) on every re-entrant lookup, so the shim's CheckRequest
// gate can recognize its own traffic on the vmcp-internal backend and admit
// it (denying any tokenless caller on that backend). server.go verifies the
// value constant-time against the same token. Exported so the server package
// references one source of truth.
const SelfHeaderName = "X-Vicegerent-Shim-Self"

// DefaultVMCPURL is the in-cluster agentgateway route this package uses for its
// re-entrant lookups: the DEDICATED :81 "internal" listener and vmcp-internal
// route, NOT the agent-facing :80 /mcp/vmcp route. The vmcp-internal
// AgentgatewayPolicy runs the required lookup methods at Request only, so the
// prompt-injection gate never fires on the shim's own lookups (the circular
// dependency this whole path breaks -- see the package RECURSION-SAFETY NOTE).
// Reachable over plain HTTP (no mTLS) -- the mTLS hop (ghostunnel) only covers
// agentgateway's own egress to the host vMCP, not this in-cluster leg.
const DefaultVMCPURL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local:81/mcp/vmcp-internal"

// mcpProtocolVersion is the MCP spec date this client speaks. Sent in
// initialize and echoed in the MCP-Protocol-Version header on every
// subsequent request per the streamable-HTTP transport spec.
const mcpProtocolVersion = "2025-06-18"

// Client is a minimal MCP client over the streamable-HTTP transport: no
// vendored MCP SDK exists in this repo's go.mod, and the wire protocol
// needed here (initialize -> initialized -> tools/call, single POST per
// call) is small enough to hand-roll rather than pull in a new dependency
// for one call site.
//
// One Client is constructed per shim process (main.go) and shared by every
// live-resolved gate, so the cached MCP session below is process-wide.
type Client struct {
	url        string
	httpClient *http.Client
	selfToken  string

	// mu guards sessionID. Lookups run concurrently (one per in-flight
	// gated tools/call, across two shim replicas' worth of goroutines), and
	// they all share this Client.
	mu sync.Mutex
	// sessionID is the cached MCP session, reused across CallTool
	// invocations so the initialize -> notifications/initialized handshake
	// stays OFF the per-lookup path. Empty means "no session yet"; it is
	// cleared whenever the server rejects it (see CallTool) so the next
	// call transparently re-handshakes.
	sessionID string
	// handshakes counts completed initialize sequences. Test-only
	// observability: it's what lets a test assert that N sequential lookups
	// cost ONE handshake rather than N, which is the entire point of the
	// cache and is otherwise invisible from CallTool's return value.
	handshakes int
}

// Option configures a Client. Kept minimal (one option today) to match the
// server package's functional-option idiom and avoid churning the existing
// New(url, nil) call sites when more knobs are added.
type Option func(*Client)

// WithSelfToken makes the Client stamp SelfHeaderName: <token> on every
// request it sends, so the shim's CheckRequest gate can recognize its own
// re-entrant lookups on the vmcp-internal backend and admit them (see the
// package RECURSION-SAFETY NOTE). Empty token omits the header; the internal
// backend's CheckRequest denies an omitted token, and main refuses to start
// without the Secret, so the CNP is never the only live lock.
func WithSelfToken(token string) Option {
	return func(c *Client) { c.selfToken = token }
}

// New constructs a Client. httpClient may be nil to use a default
// *http.Client{} (plain HTTP, no TLS config -- see DefaultVMCPURL doc).
// Per-call timeouts are enforced via context, not a client-wide Timeout, so
// callers control the budget explicitly (see CallTool).
func New(url string, httpClient *http.Client, opts ...Option) *Client {
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	c := &Client{url: url, httpClient: httpClient}
	for _, o := range opts {
		o(c)
	}
	return c
}

// jsonrpcRequest and jsonrpcResponse are the minimal JSON-RPC 2.0 envelope
// this client needs; no batching, no bidirectional server->client requests.
type jsonrpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      int    `json:"id"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type jsonrpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      int             `json:"id"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *jsonrpcError   `json:"error,omitempty"`
}

type jsonrpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *jsonrpcError) Error() string {
	return fmt.Sprintf("mcp error %d: %s", e.Code, e.Message)
}

// CallToolResult is the tools/call result shape this package needs: the
// content blocks a tool returns. Notion's tools return their payload as a
// single text block of enhanced Markdown (see docs/available-mcp-tools/
// notion.yaml); other content types (image/resource) are ignored here.
type CallToolResult struct {
	Content []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
	IsError bool `json:"isError"`
}

// Text concatenates every text content block, which is all the ancestry
// walk needs from a notion_notion-fetch response.
func (r *CallToolResult) Text() string {
	var b strings.Builder
	for _, c := range r.Content {
		if c.Type == "text" {
			b.WriteString(c.Text)
		}
	}
	return b.String()
}

// callToolMetaName is the vMCP tool-discovery optimizer's (thv vmcp serve
// --optimizer, on by DEFAULT in this deployment -- see host/mcp/README.md
// "Tool discovery optimizer" and VMCP_OPTIMIZER in vicegerent_mcp.py) meta-tool
// name. With it on, vMCP's own tools/list exposes ONLY find_tool/call_tool --
// there is no raw notion_notion-fetch tool at that level to call directly.
// Every real tool invocation, including this package's own outbound ancestry
// lookup, must go through call_tool{tool_name, parameters} or vMCP returns
// "tool not found". This mirrors, on the outbound side, server.go's callToolMeta
// unwrapping does on the inbound side for calls arriving at the shim.
const callToolMetaName = "call_tool"

// CallTool returns the named tool's result, reusing a cached MCP session so
// the initialize -> notifications/initialized handshake is NOT paid per call.
// The tools/call itself is always wrapped in the optimizer's call_tool
// meta-tool (see callToolMetaName) -- there is no direct/unwrapped path in
// this deployment, so this package doesn't try to detect or fall back; if a
// future deployment ever disables the optimizer, this wrapping becomes a
// no-op shape mismatch that would need revisiting, not a silent failure
// (vMCP would still need to expose call_tool for this to work at all).
//
// Session reuse keeps steady-state lookups to one round trip. Repeating the
// initialize handshake for every lookup can exhaust the authorization
// deadline before the tool call completes.
//
// A cached session can still be rejected -- the server restarts, evicts idle
// sessions, or the gateway rolls. That surfaces as a non-200 (typically 404
// Not Found on an unknown Mcp-Session-Id), so on the first such failure the
// cached session is dropped and the whole sequence retried once against a
// fresh handshake. Exactly one retry: a second failure is a real upstream
// problem, not a stale session, and must not become an unbounded loop inside
// a gate that is holding an agent's call open.
//
// ctx's deadline governs the whole sequence including any retry -- exceeding
// it, any non-200 on the retry, any malformed JSON-RPC envelope, or a
// JSON-RPC error response all return a non-nil error; there is no
// partial/best-effort result.
func (c *Client) CallTool(ctx context.Context, tool string, arguments map[string]any) (*CallToolResult, error) {
	result, sessionID, err := c.callToolOnce(ctx, tool, arguments)
	if err == nil || !errors.Is(err, errSessionRejected) {
		return result, err
	}
	// Stale cached session: drop it and try once more with a fresh handshake.
	// Guarded so a concurrent caller that already replaced the session isn't
	// clobbered.
	c.invalidateSession(sessionID)
	result, _, err = c.callToolOnce(ctx, tool, arguments)
	if err != nil {
		return nil, err
	}
	return result, nil
}

// callToolOnce runs one attempt: reuse-or-establish a session, then issue the
// wrapped tools/call. A rejected session is reported as errSessionRejected so
// CallTool can distinguish "retry with a new session" from a genuine failure.
func (c *Client) callToolOnce(ctx context.Context, tool string, arguments map[string]any) (*CallToolResult, string, error) {
	sessionID, err := c.session(ctx)
	if err != nil {
		return nil, "", fmt.Errorf("upstream initialize: %w", err)
	}

	resp, err := c.doRPC(ctx, sessionID, jsonrpcRequest{
		JSONRPC: "2.0",
		ID:      2,
		Method:  "tools/call",
		Params: map[string]any{
			"name": callToolMetaName,
			"arguments": map[string]any{
				"tool_name":  tool,
				"parameters": arguments,
			},
		},
	})
	if err != nil {
		return nil, sessionID, fmt.Errorf("upstream tools/call %s: %w", tool, err)
	}
	if resp.Error != nil {
		return nil, sessionID, fmt.Errorf("upstream tools/call %s: %w", tool, resp.Error)
	}

	var result CallToolResult
	if err := json.Unmarshal(resp.Result, &result); err != nil {
		return nil, sessionID, fmt.Errorf("upstream tools/call %s: malformed result: %w", tool, err)
	}
	if result.IsError {
		return nil, sessionID, fmt.Errorf("upstream tools/call %s: tool reported an error: %s", tool, result.Text())
	}
	return &result, sessionID, nil
}

// session returns the cached MCP session id, performing the handshake only if
// there isn't one yet. The lock is held across the handshake so a burst of
// concurrent first-lookups produces ONE initialize rather than one each --
// the thundering-herd case that would otherwise reproduce the original
// latency problem on every shim restart.
func (c *Client) session(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.sessionID != "" {
		return c.sessionID, nil
	}
	sessionID, err := c.initialize(ctx)
	if err != nil {
		return "", err
	}
	c.sessionID = sessionID
	c.handshakes++
	return sessionID, nil
}

// invalidateSession clears the rejected session without clobbering a session
// that another caller has already established.
func (c *Client) invalidateSession(rejectedID string) {
	c.mu.Lock()
	if c.sessionID == rejectedID {
		c.sessionID = ""
	}
	c.mu.Unlock()
}

// handshakeCount reports how many initialize sequences this Client has
// completed. Test-only: the whole point of the session cache is that this
// stays at 1 across many lookups, which no other observable exposes.
func (c *Client) handshakeCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.handshakes
}

// initialize opens a new MCP session and returns the Mcp-Session-Id the
// server assigned (per the streamable-HTTP transport spec), sending the
// required notifications/initialized notification before returning.
func (c *Client) initialize(ctx context.Context) (string, error) {
	req := jsonrpcRequest{
		JSONRPC: "2.0",
		ID:      1,
		Method:  "initialize",
		Params: map[string]any{
			"protocolVersion": mcpProtocolVersion,
			"capabilities":    map[string]any{},
			"clientInfo": map[string]any{
				"name":    "mcp-cerbos-shim",
				"version": "1",
			},
		},
	}
	sessionID, resp, err := c.postJSONRPC(ctx, "", req)
	if err != nil {
		return "", err
	}
	if resp.Error != nil {
		return "", resp.Error
	}

	// Fire-and-forget notification; no response body per JSON-RPC (no id).
	notif := struct {
		JSONRPC string `json:"jsonrpc"`
		Method  string `json:"method"`
	}{JSONRPC: "2.0", Method: "notifications/initialized"}
	body, err := json.Marshal(notif)
	if err != nil {
		return "", fmt.Errorf("marshal initialized notification: %w", err)
	}
	if err := c.post(ctx, sessionID, body); err != nil {
		return "", fmt.Errorf("send initialized notification: %w", err)
	}
	return sessionID, nil
}

// doRPC sends one JSON-RPC request on an already-initialized session.
func (c *Client) doRPC(ctx context.Context, sessionID string, req jsonrpcRequest) (*jsonrpcResponse, error) {
	_, resp, err := c.postJSONRPC(ctx, sessionID, req)
	return resp, err
}

// setHeaders applies the streamable-HTTP transport headers every request
// this client sends needs, plus SelfHeaderName when a self-token is
// configured (WithSelfToken) so the shim recognizes its own re-entrant
// lookups. Shared by postJSONRPC and post so the two stay in lockstep.
func (c *Client) setHeaders(req *http.Request, sessionID string) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("MCP-Protocol-Version", mcpProtocolVersion)
	if sessionID != "" {
		req.Header.Set("Mcp-Session-Id", sessionID)
	}
	if c.selfToken != "" {
		req.Header.Set(SelfHeaderName, c.selfToken)
	}
}

// postJSONRPC marshals req, POSTs it, and parses the (possibly
// session-establishing) response. Returns the session id from the
// Mcp-Session-Id response header, if any.
func (c *Client) postJSONRPC(ctx context.Context, sessionID string, req jsonrpcRequest) (string, *jsonrpcResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return "", nil, fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return "", nil, fmt.Errorf("build request: %w", err)
	}
	c.setHeaders(httpReq, sessionID)

	httpResp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return "", nil, fmt.Errorf("do request: %w", err)
	}
	defer httpResp.Body.Close()

	respBody, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return "", nil, fmt.Errorf("read response body: %w", err)
	}
	if httpResp.StatusCode != http.StatusOK {
		// 404/400 on a request that carried a session id means the server no
		// longer knows that session (restart, idle eviction, gateway roll).
		// Flag it so CallTool can re-handshake once instead of failing the
		// gate closed on a recoverable condition. Only when a session was
		// actually presented -- the same status on the initialize call itself
		// is a genuine upstream failure with nothing to retry.
		if sessionID != "" && (httpResp.StatusCode == http.StatusNotFound || httpResp.StatusCode == http.StatusBadRequest) {
			return "", nil, fmt.Errorf("%w: %d: %s", errSessionRejected, httpResp.StatusCode, truncate(string(respBody), 300))
		}
		return "", nil, fmt.Errorf("non-200 response: %d: %s", httpResp.StatusCode, truncate(string(respBody), 300))
	}

	newSessionID := httpResp.Header.Get("Mcp-Session-Id")
	if newSessionID == "" {
		newSessionID = sessionID
	}

	rpcBody, err := extractJSONRPCBody(httpResp.Header.Get("Content-Type"), respBody)
	if err != nil {
		return "", nil, err
	}

	var resp jsonrpcResponse
	if err := json.Unmarshal(rpcBody, &resp); err != nil {
		return "", nil, fmt.Errorf("malformed JSON-RPC response: %w", err)
	}
	return newSessionID, &resp, nil
}

// post sends a body with no response expected (JSON-RPC notification).
func (c *Client) post(ctx context.Context, sessionID string, body []byte) error {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	c.setHeaders(httpReq, sessionID)

	httpResp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("do request: %w", err)
	}
	defer httpResp.Body.Close()
	io.Copy(io.Discard, httpResp.Body) //nolint:errcheck // draining is best-effort, not load-bearing
	// A notification legitimately gets 202 Accepted (no body) per the
	// streamable-HTTP spec; some servers may also reply 200. Anything else
	// is a real failure.
	if httpResp.StatusCode != http.StatusOK && httpResp.StatusCode != http.StatusAccepted {
		return fmt.Errorf("non-200/202 response: %d", httpResp.StatusCode)
	}
	return nil
}

// extractJSONRPCBody handles both response shapes the streamable-HTTP
// transport allows: a plain application/json body, or a single-event
// text/event-stream body carrying one "data: <json>" line.
func extractJSONRPCBody(contentType string, body []byte) ([]byte, error) {
	if !strings.HasPrefix(strings.TrimSpace(contentType), "text/event-stream") {
		return body, nil
	}
	for _, line := range strings.Split(string(body), "\n") {
		line = strings.TrimSpace(line)
		if data, ok := strings.CutPrefix(line, "data:"); ok {
			return []byte(strings.TrimSpace(data)), nil
		}
	}
	return nil, fmt.Errorf("SSE response carried no data: line")
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "...(truncated)"
}
