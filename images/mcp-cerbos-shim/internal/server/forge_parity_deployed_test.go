package server

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// This file turns the GitHub/GitLab parity audit into an executable check.
//
// AGENTS.md requires the two forge policies to stay in lockstep, but that rule
// was enforced only by a human reading two files and a hand-maintained
// "NOT ported" list in resource_gitlab.yaml's header. That is exactly the shape
// of guarantee that rots: the header can stay confident while the tool
// allowlist underneath it changes. These tests assert the properties the audit
// actually established, against the SHIPPED allowlist and the SHIPPED mapping,
// so a future allowlist edit that opens an ungated write fails CI instead of
// being discovered in production.

// toolhiveServers is the subset of host/mcp/toolhive-servers.json these tests
// read: each server's name and its vMCP tool allowlist.
type toolhiveServers struct {
	Servers []struct {
		Name  string   `json:"name"`
		Tools []string `json:"tools"`
	} `json:"servers"`
}

// loadToolAllowlist returns the allowlisted tool names for one server, prefixed
// the way vMCP presents them to the shim (e.g. "gitlab_create_issue"). Skips
// rather than fails on a bare checkout with no host/ tree, matching
// renderDeployedMapping's stance on a missing chart.
func loadToolAllowlist(t *testing.T, server string) map[string]bool {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatalf("resolve repo root: %v", err)
	}
	p := filepath.Join(root, "host", "mcp", "toolhive-servers.json")
	raw, err := os.ReadFile(p)
	if err != nil {
		t.Skipf("%s not present: %v", p, err)
	}
	var ts toolhiveServers
	if err := json.Unmarshal(raw, &ts); err != nil {
		t.Fatalf("parse %s: %v", p, err)
	}
	for _, s := range ts.Servers {
		if s.Name != server {
			continue
		}
		out := make(map[string]bool, len(s.Tools))
		for _, tool := range s.Tools {
			out[server+"_"+tool] = true
		}
		return out
	}
	t.Fatalf("server %q not found in %s", server, p)
	return nil
}

// TestForgeParity_EveryAllowlistedToolIsMapped is the load-bearing one. The
// vmcp backend is defaultAction: allow, so a tool that is allowlisted but has
// NO mapping.yaml entry is not ""allowed by policy"" -- it bypasses Cerbos
// entirely and is scoped by nothing. Adding a tool to the allowlist without a
// mapping entry is therefore silently ungated, on either forge.
//
// The two documented exceptions are asserted explicitly rather than skipped, so
// removing one from the allowlist (the real fix) also updates this list:
//   - gitlab_mark_todo_done / gitlab_mark_all_todos_done carry no project id in
//     their args at all, so there is nothing to scope them by. Documented as
//     the standing unscopable gap in resource_gitlab.yaml.
//   - github_get_me takes no arguments and returns the authenticated user; it
//     names no repo to scope.
func TestForgeParity_EveryAllowlistedToolIsMapped(t *testing.T) {
	m := deployedMapping(t)
	b, ok := m.Backends["vmcp"]
	if !ok {
		t.Fatal("rendered mapping has no vmcp backend")
	}

	knownUnscopable := map[string]string{
		"gitlab_mark_todo_done":      "carries no project id to scope by (documented gap)",
		"gitlab_mark_all_todos_done": "carries no project id to scope by (documented gap)",
		"github_get_me":              "takes no args and names no repo",
	}

	for _, forge := range []string{"gitlab", "github"} {
		t.Run(forge, func(t *testing.T) {
			allowed := loadToolAllowlist(t, forge)
			var unmapped []string
			for tool := range allowed {
				if _, mapped := b.Tools[tool]; mapped {
					continue
				}
				if _, known := knownUnscopable[tool]; known {
					continue
				}
				unmapped = append(unmapped, tool)
			}
			sort.Strings(unmapped)
			if len(unmapped) > 0 {
				t.Errorf("allowlisted but UNMAPPED (ungated on a defaultAction:allow backend): %v\n"+
					"either map these in charts/mcp-cerbos-shim/files/mapping.yaml or remove them from "+
					"host/mcp/toolhive-servers.json", unmapped)
			}
			// The documented exceptions must still actually be in the allowlist;
			// if one was removed, delete it from knownUnscopable too.
			for tool, why := range knownUnscopable {
				if !isForgeTool(tool, forge) {
					continue
				}
				if !allowed[tool] {
					t.Errorf("%s is listed as a known-unscopable exception (%s) but is no longer "+
						"allowlisted -- drop it from knownUnscopable", tool, why)
				}
			}
		})
	}
}

// TestForgeParity_DangerousToolsAbsentOnBothForges asserts the capability
// removals that resource_gitlab.yaml's header claims are "mirrored in the TOOL
// ALLOWLIST instead of as rules here". That claim is only true while the tools
// stay absent, and absence is invisible -- nothing fails when a tool quietly
// comes back. Removal is strictly stronger than a Cerbos rule, so it is worth
// pinning.
//
// Covers the three classes the operator explicitly removed on BOTH forges:
// repository file/branch writes (the bot uses git over SSH instead), merging,
// and every comment/review WRITE surface (the operator does not want the bot
// leaving comment text under their identity on either forge).
func TestForgeParity_DangerousToolsAbsentOnBothForges(t *testing.T) {
	cases := map[string][]string{
		"gitlab": {
			// file/branch writes
			"gitlab_create_or_update_file", "gitlab_push_files",
			"gitlab_create_branch", "gitlab_delete_branch",
			// merge / approve
			"gitlab_merge_merge_request", "gitlab_approve_merge_request",
			// issue writes (issue surface is read-only on both forges)
			"gitlab_create_issue", "gitlab_update_issue", "gitlab_create_issue_link",
			// comment/note/discussion WRITE surface
			"gitlab_create_note", "gitlab_create_merge_request_note",
			"gitlab_update_merge_request_note", "gitlab_create_merge_request_thread",
			"gitlab_create_draft_note", "gitlab_update_draft_note",
			"gitlab_publish_draft_note", "gitlab_delete_draft_note",
			"gitlab_bulk_publish_draft_notes",
		},
		"github": {
			"github_create_or_update_file", "github_push_files",
			"github_create_branch", "github_delete_branch",
			"github_merge_pull_request",
			// issue writes (issue surface is read-only on both forges)
			"github_issue_write", "github_sub_issue_write", "github_assign_copilot_to_issue",
			"github_add_issue_comment", "github_create_pull_request_review",
			"github_submit_pending_pull_request_review",
			"github_create_and_submit_pull_request_review",
			"github_add_pull_request_review_comment_to_pending_review",
		},
	}

	for forge, banned := range cases {
		t.Run(forge, func(t *testing.T) {
			allowed := loadToolAllowlist(t, forge)
			for _, tool := range banned {
				if allowed[tool] {
					t.Errorf("%s is back in the tool allowlist -- this capability was deliberately "+
						"REMOVED (see resource_%s.yaml header). Removing the tool is the enforcement; "+
						"if it must return, it needs a Cerbos rule on both forges.", tool, forge)
				}
			}
		})
	}
}

// TestForgeParity_NoteWriteSurfaceIsReadOnly generalizes the comment-removal
// check: rather than listing known note-write tool names, it asserts that NO
// allowlisted note/discussion/thread tool on either forge has a write-shaped
// verb prefix. This catches a renamed or newly-added comment tool that a
// hardcoded ban list would miss -- the operator's rule is about the capability,
// not about specific spellings.
func TestForgeParity_NoteWriteSurfaceIsReadOnly(t *testing.T) {
	writeVerbs := []string{"create_", "update_", "delete_", "publish_", "bulk_", "add_", "submit_", "reply_"}
	for _, forge := range []string{"gitlab", "github"} {
		t.Run(forge, func(t *testing.T) {
			for tool := range loadToolAllowlist(t, forge) {
				bare := tool[len(forge)+1:]
				if !mentionsComment(bare) {
					continue
				}
				for _, v := range writeVerbs {
					if len(bare) >= len(v) && bare[:len(v)] == v {
						t.Errorf("%s looks like a comment/note WRITE tool but is allowlisted; "+
							"the operator's rule is that the bot leaves no comment text under their "+
							"identity on either forge", tool)
					}
				}
			}
		})
	}
}

func isForgeTool(tool, forge string) bool {
	return len(tool) > len(forge) && tool[:len(forge)+1] == forge+"_"
}

func mentionsComment(bare string) bool {
	for _, kw := range []string{"note", "discussion", "thread", "comment", "review"} {
		if containsSub(bare, kw) {
			return true
		}
	}
	return false
}

func containsSub(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func init() {
	// Fail fast if the repo layout assumption changes, rather than silently
	// skipping every test in this file.
	if _, err := filepath.Abs(filepath.Join("..", "..", "..", "..")); err != nil {
		panic(fmt.Sprintf("forge parity tests: cannot resolve repo root: %v", err))
	}
}
