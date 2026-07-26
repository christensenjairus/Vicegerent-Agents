package upstream

import (
	"context"
	"errors"
	"testing"
)

// fakeCaller is a ToolCaller stub: it returns a canned notion-fetch text (or
// an error) without any network, so ancestry parsing is tested against literal
// fixtures. It also records the tool/args it was called with.
type fakeCaller struct {
	text      string
	err       error
	gotTool   string
	gotArgs   map[string]any
	callCount int
}

func (f *fakeCaller) CallTool(_ context.Context, tool string, args map[string]any) (*CallToolResult, error) {
	f.callCount++
	f.gotTool = tool
	f.gotArgs = args
	if f.err != nil {
		return nil, f.err
	}
	return &CallToolResult{Content: []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}{{Type: "text", Text: f.text}}}, nil
}

const scratchpadID = "393de8859710809c9f5ec57a91d2c81a" // pragma: allowlist secret

// realFormatUnderScratchpad matches the live notion-fetch shape: an XML-ish
// <page> wrapper whose <ancestor-path> carries the full flattened chain to
// root. Here the deep page's only ancestor is the Scratchpad page itself
// (Notion flattens intermediate pages, so even a deeply nested page shows the
// top-level ancestor directly).
const realFormatUnderScratchpad = `<page url="https://app.notion.com/p/aaaa1111bbbb2222cccc3333dddd4444" title="Deep Notes">
<ancestor-path>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
<content>
some body text
</content>
</page>`

// multipleAncestors shows more than one ancestor in the chain; the match is
// the second entry, proving all entries (not just the first) are scanned.
const multipleAncestors = `<page url="https://app.notion.com/p/leaf0000leaf0000leaf0000leaf0000" title="Leaf">
<ancestor-path>
<parent-page url="https://app.notion.com/p/ffff9999ffff9999ffff9999ffff9999" title="Some Project"/>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
</page>`

// rootPageEmptyAncestry is a top-level/root page: an empty ancestor-path.
const rootPageEmptyAncestry = `<page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad">
<ancestor-path></ancestor-path>
<content>root</content>
</page>`

// underDifferentTree is a valid page whose ancestors don't include Scratchpad.
const underDifferentTree = `<page url="https://app.notion.com/p/1234abcd1234abcd1234abcd1234abcd" title="Elsewhere">
<ancestor-path>
<parent-page url="https://app.notion.com/p/9999000099990000999900009999abcd" title="Other Folder"/>
</ancestor-path>
</page>`

// deepNestedUnderTeamFolder covers Notion's numbered tags for ancestors
// beyond the immediate parent. The allowed ancestor appears at depth three.
const deepNestedUnderTeamFolder = `<page url="https://app.notion.com/p/1ccde885971081c2a0a8caa913e4e3c0" icon="tag">
<ancestor-path>
<parent-page url="https://app.notion.com/p/1ccde885971081a5a020e73928f42fbe" title=""/>
<ancestor-2-page url="https://app.notion.com/p/1ccde8859710816d8ef8d536fd6daa30" title=""/>
<ancestor-3-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
</page>`

// realWireEnvelope covers the production notion-fetch shape: a JSON object
// whose text field contains escaped page markup. extractNotionFetchText must
// unwrap this layer before ancestry matching.
const realWireEnvelope = `{"metadata":{"type":"page"},"title":"post-merge validation - correct parent","url":"https://app.notion.com/p/395de8859710818c9345eaf50f004790","text":"Here is the result of \"view\" for the Page with URL https://app.notion.com/p/395de8859710818c9345eaf50f004790 as of 2026-07-06T03:15:36.560Z:\n<page url=\"https://app.notion.com/p/395de8859710818c9345eaf50f004790\">\n<ancestor-path>\n<parent-page url=\"https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a\" title=\"Scratchpad\"/>\n</ancestor-path>\n<properties>\n{\"title\":\"post-merge validation - correct parent\"}\n</properties>\n<blank-page>This page is blank and has no content.</blank-page>\n</page>"}`

// realWireEnvelopeNotUnderScratchpad is the same real JSON-wrapped shape but
// with an ancestor-path pointing somewhere else, so extractNotionFetchText
// unwrapping is exercised on both the true and false branches.
const realWireEnvelopeNotUnderScratchpad = `{"metadata":{"type":"page"},"title":"Elsewhere","url":"https://app.notion.com/p/1234abcd1234abcd1234abcd1234abcd","text":"<page url=\"https://app.notion.com/p/1234abcd1234abcd1234abcd1234abcd\">\n<ancestor-path>\n<parent-page url=\"https://app.notion.com/p/9999000099990000999900009999abcd\" title=\"Other Folder\"/>\n</ancestor-path>\n</page>"}`

// underDatabaseParent covers wiki database ancestry, which uses a
// <parent-database> tag instead of <parent-page>.
const underDatabaseParent = `<page url="https://app.notion.com/p/395588d8909f809293facb344df0b71d">
<ancestor-path>
<parent-database url="https://app.notion.com/p/1de588d8909f80458ad6c0a831284768" title="Engineering Wiki"/>
<ancestor-2-page url="https://app.notion.com/p/fc3c172feed9463682bef6d40d96bd54" title=""/>
</ancestor-path>
</page>`

const databaseParentID = "1de588d8909f80458ad6c0a831284768" // pragma: allowlist secret

func TestPageIsUnderAnyAncestor(t *testing.T) {
	cases := []struct {
		name    string
		text    string
		callErr error
		want    bool
		wantErr bool
	}{
		{name: "real format under scratchpad", text: realFormatUnderScratchpad, want: true},
		{name: "match is not the first ancestor", text: multipleAncestors, want: true},
		{name: "root page empty ancestry is not under scratchpad", text: rootPageEmptyAncestry, want: false},
		{name: "populated but no matching ancestor", text: underDifferentTree, want: false},
		{name: "deep nested ancestor (ancestor-3-page tag, not parent-page)", text: deepNestedUnderTeamFolder, want: true},
		{name: "real JSON-wrapped wire shape under scratchpad", text: realWireEnvelope, want: true},
		{name: "real JSON-wrapped wire shape not under scratchpad", text: realWireEnvelopeNotUnderScratchpad, want: false},
		{name: "lookup error fails closed", callErr: errors.New("boom"), wantErr: true},
		{name: "malformed result with no ancestor-path block errors", text: "just some text, no tags", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeCaller{text: tc.text, err: tc.callErr}
			got, err := PageIsUnderAnyAncestor(context.Background(), f, "leaf-page-id", []string{scratchpadID})
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil (result=%v)", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("PageIsUnderAnyAncestor = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestPageIsUnderAnyAncestor_CallsFetchOnce guards the "single fetch is
// authoritative" invariant: exactly one notion_notion-fetch call, on the leaf
// page id, no parent-walk loop.
func TestPageIsUnderAnyAncestor_CallsFetchOnce(t *testing.T) {
	f := &fakeCaller{text: realFormatUnderScratchpad}
	if _, err := PageIsUnderAnyAncestor(context.Background(), f, "leaf-page-id", []string{scratchpadID}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if f.callCount != 1 {
		t.Errorf("expected exactly 1 fetch, got %d", f.callCount)
	}
	if f.gotTool != notionFetchTool {
		t.Errorf("fetched tool = %q, want %q", f.gotTool, notionFetchTool)
	}
	if f.gotArgs["id"] != "leaf-page-id" {
		t.Errorf("fetch args id = %v, want leaf-page-id", f.gotArgs["id"])
	}
}

// TestPageIsUnderAnyAncestor_NormalizesDashes proves a dashed ancestor id from the
// caller matches an undashed id in the fetched ancestor-path (and vice versa).
func TestPageIsUnderAnyAncestor_NormalizesDashes(t *testing.T) {
	dashedTarget := "393de885-9710-809c-9f5e-c57a91d2c81a" // same id as scratchpadID, dashed
	f := &fakeCaller{text: realFormatUnderScratchpad}
	got, err := PageIsUnderAnyAncestor(context.Background(), f, "leaf", []string{dashedTarget})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got {
		t.Errorf("expected dashed target to match undashed ancestor id")
	}
}

// TestPageIsUnderAnyAncestor_EmptyTargetErrors: an empty ancestor id is a
// misconfiguration, not a valid "match nothing"; fail closed.
func TestPageIsUnderAnyAncestor_EmptyTargetErrors(t *testing.T) {
	f := &fakeCaller{text: realFormatUnderScratchpad}
	if _, err := PageIsUnderAnyAncestor(context.Background(), f, "leaf", []string{""}); err == nil {
		t.Fatal("expected error for empty ancestor page id")
	}
}

// TestPageIsUnderAnyAncestor_DatabaseParentTag proves a wiki database parent
// matches through <parent-database> rather than <parent-page>.
func TestPageIsUnderAnyAncestor_DatabaseParentTag(t *testing.T) {
	f := &fakeCaller{text: underDatabaseParent}
	got, err := PageIsUnderAnyAncestor(context.Background(), f, "leaf-page-id", []string{databaseParentID})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got {
		t.Errorf("expected page under a <parent-database> tag to match that database's id")
	}
}
