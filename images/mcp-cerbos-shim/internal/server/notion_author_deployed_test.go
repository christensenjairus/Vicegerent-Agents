package server

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path for notion_notion-update-page/notion_notion-create-comment, with a
// FAKE upstream (no network) standing in for the live vMCP notion-fetch +
// notion-search calls the author-resolution gate makes. They prove the gate
// wiring: a page the fake resolves as operator-authored passes through with
// no mismatch attr, a non-matching page forwards pageAuthorMismatch=true to
// Cerbos, a lookup failure fails closed, and an unconfigured gate fails
// closed. The deny decision for a mismatched page itself (deny-not-own-page)
// is proven separately by defs/notion_test.yaml.

const authorTestPageID = "abc123def456abc123def456abc123d" // pragma: allowlist secret

const notionAuthorFetchWithTitle = `<page url="https://app.notion.com/p/abc123def456abc123def456abc123d">
<ancestor-path>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
<properties>
{"title":"Leaf"}
</properties>
</page>`

const notionAuthorSearchMatch = `{"results":[{"id":"abc123def456abc123def456abc123d","title":"Leaf"}]}` // pragma: allowlist secret
const notionAuthorSearchNoMatch = `{"results":[{"id":"someotherpageid00000000000000000","title":"Leaf"}]}`

// notionAuthorFetchWithOwnerProperty is a database-row page whose <properties>
// block carries an "Owner" person property mentioning newNotionAuthorServer's
// wired operator ("operator-user-id") -- see notion_author.go's
// ownerPropertyMentionsOperator doc comment for the live-verified rendering.
const notionAuthorFetchWithOwnerProperty = `<page url="https://app.notion.com/p/abc123def456abc123def456abc123d">
<ancestor-path>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
<properties>
{"title":"Leaf","Owner":["<mention-user url=\"user://operator-user-id\"></mention-user>"]}
</properties>
</page>`

// notionAuthorFakeUpstream is a server-package ToolCaller stub for the
// author gate's two-call sequence (fetch then creator-filtered search) --
// distinct from permissiveNotionAuthorUpstream (notion_ancestry_deployed_test.go),
// since these tests need independently controllable fetch/search
// results/errors rather than an always-succeeds stub.
type notionAuthorFakeUpstream struct {
	fetchText  string
	fetchErr   error
	searchText string
	searchErr  error

	fetchCalls  int
	searchCalls int
}

func (f *notionAuthorFakeUpstream) CallTool(_ context.Context, tool string, _ map[string]any) (*upstream.CallToolResult, error) {
	switch tool {
	case "notion_notion-fetch":
		f.fetchCalls++
		if f.fetchErr != nil {
			return nil, f.fetchErr
		}
		return &upstream.CallToolResult{Content: []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}{{Type: "text", Text: f.fetchText}}}, nil
	case "notion_notion-search":
		f.searchCalls++
		if f.searchErr != nil {
			return nil, f.searchErr
		}
		return &upstream.CallToolResult{Content: []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}{{Type: "text", Text: f.searchText}}}, nil
	default:
		return nil, fmt.Errorf("unexpected tool: %s", tool)
	}
}

// newNotionAuthorServer wires a permissive ancestry gate (these tests are
// about the AUTHOR gate, not ancestry -- see notion_ancestry_deployed_test.go
// for that gate's own dedicated tests) alongside the author fake under test.
func newNotionAuthorServer(t *testing.T, d *stubDecider, authorUp upstream.ToolCaller) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithNotionAncestry(&fakeUpstream{text: fetchUnderScratchpad}, []string{testScratchpadID}),
		WithNotionPageAuthor(authorUp, "operator-user-id"))
}

func TestDeployedNotionMapping_UpdatePageAuthoredByOperatorPassesWithNoMismatchAttr(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &notionAuthorFakeUpstream{fetchText: notionAuthorFetchWithTitle, searchText: notionAuthorSearchMatch}
	s := newNotionAuthorServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": authorTestPageID, "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: page resolved as operator-authored")
	}
	if up.fetchCalls != 1 || up.searchCalls != 1 {
		t.Errorf("expected exactly one fetch and one search call, got fetch=%d search=%d", up.fetchCalls, up.searchCalls)
	}
	if _, ok := d.gotAttr["pageAuthorMismatch"]; ok {
		t.Errorf("expected no pageAuthorMismatch attr for an operator-authored page, got %v", d.gotAttr["pageAuthorMismatch"])
	}
}

// TestDeployedNotionMapping_CreateCommentNotAuthoredByOperatorForwardsMismatch
// proves the GATE's half of the contract: it resolves the page's REAL author
// (not the operator) and forwards pageAuthorMismatch=true to Cerbos. The
// actual allow/deny decision is Cerbos policy's job (defs/notion_test.yaml's
// deny-not-own-page case) -- this uses stubDecider (a fixed verdict) only to
// confirm what the gate SENDS.
func TestDeployedNotionMapping_CreateCommentNotAuthoredByOperatorForwardsMismatch(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &notionAuthorFakeUpstream{fetchText: notionAuthorFetchWithTitle, searchText: notionAuthorSearchNoMatch}
	s := newNotionAuthorServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-comment",
			map[string]any{"page_id": authorTestPageID, "markdown": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in notion_test.yaml)")
	}
	if mismatch, _ := d.gotAttr["pageAuthorMismatch"].(bool); !mismatch {
		t.Errorf("Cerbos saw pageAuthorMismatch=%v, want true", d.gotAttr["pageAuthorMismatch"])
	}
}

// TestDeployedNotionMapping_UpdatePageOwnerPropertyMatchPassesWithoutSearchCall
// proves the Owner-property OR-fallback is wired end-to-end at the shim
// level: a page the operator didn't create but whose own Owner property
// names them passes with no pageAuthorMismatch attr, and -- since the check
// runs against the already-fetched properties block -- never makes the
// creator-filtered search call at all.
func TestDeployedNotionMapping_UpdatePageOwnerPropertyMatchPassesWithoutSearchCall(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &notionAuthorFakeUpstream{fetchText: notionAuthorFetchWithOwnerProperty}
	s := newNotionAuthorServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": authorTestPageID, "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: page's own Owner property names the operator")
	}
	if up.fetchCalls != 1 || up.searchCalls != 0 {
		t.Errorf("expected one fetch and no search call (Owner property match short-circuits), got fetch=%d search=%d", up.fetchCalls, up.searchCalls)
	}
	if _, ok := d.gotAttr["pageAuthorMismatch"]; ok {
		t.Errorf("expected no pageAuthorMismatch attr for an Owner-property-matched page, got %v", d.gotAttr["pageAuthorMismatch"])
	}
}

func TestDeployedNotionMapping_AuthorLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &notionAuthorFakeUpstream{fetchErr: errors.New("upstream timeout")}
	s := newNotionAuthorServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": authorTestPageID, "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the author lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedNotionMapping_UnconfiguredAuthorGateFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// Only the ancestry gate is wired -- production's main.go always wires
	// the author gate too, so reaching here unconfigured means a broken
	// deploy, not a license to allow an unscoped page edit.
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithNotionAncestry(&fakeUpstream{text: fetchUnderScratchpad}, []string{testScratchpadID}))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": authorTestPageID, "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny with the author gate unconfigured (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached with the gate unconfigured, got %d calls", d.calls)
	}
}

// TestDeployedNotionMapping_CreatePagesDoesNotTriggerAuthorGate proves
// create-pages (a brand-new page has no prior-ownership question) is
// untouched by this gate.
func TestDeployedNotionMapping_CreatePagesDoesNotTriggerAuthorGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &notionAuthorFakeUpstream{fetchErr: errors.New("must not be called")}
	s := newNotionAuthorServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-pages",
			map[string]any{
				"pages":  []any{map[string]any{"properties": map[string]any{"title": "t"}}},
				"parent": map[string]any{"page_id": testScratchpadID},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass, got deny: %s", res.GetError().GetReason())
	}
	if up.fetchCalls != 0 || up.searchCalls != 0 {
		t.Errorf("create-pages must not trigger the author lookup gate, got fetch=%d search=%d", up.fetchCalls, up.searchCalls)
	}
}
