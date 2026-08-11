package eval

import "testing"

// GitLab has no draft boolean - draft status comes from the title prefix
// (verified live: draft:true is silently ignored, "Draft: x" is honoured), so
// these cases pin the rewrite that replaced the no-op force: {draft: true}.
func TestGitlabDraftTitle(t *testing.T) {
	tests := []struct {
		name  string
		title string
		want  string
	}{
		{
			name:  "plain title gets the draft prefix",
			title: "Fix the thing",
			want:  "Draft: Fix the thing",
		},
		{
			name:  "absent title yields no override",
			title: "",
			want:  "",
			// update_merge_request's title is optional. Forcing a title here
			// would overwrite the MR's real title with "Draft: ".
		},
		{
			name:  "whitespace-only title is treated as absent",
			title: "   ",
			want:  "",
		},
		{
			name:  "already-drafted title is left alone",
			title: "Draft: Fix the thing",
			want:  "",
		},
		{
			name:  "already-drafted title, lowercase marker",
			title: "draft: fix the thing",
			want:  "",
		},
		{
			name:  "already-drafted title, mixed-case marker",
			title: "DRAFT: Fix the thing",
			want:  "",
		},
		{
			name:  "legacy WIP marker is respected too",
			title: "WIP: Fix the thing",
			want:  "",
			// GitLab still accepts WIP: as a draft marker; prefixing it would
			// produce "Draft: WIP: ..." for no benefit.
		},
		{
			name:  "a title merely containing the word draft is still prefixed",
			title: "Update the draft policy doc",
			want:  "Draft: Update the draft policy doc",
			// Prefix check, not a substring check.
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := gitlabDraftTitle(tc.title); got != tc.want {
				t.Errorf("gitlabDraftTitle(%q) = %q, want %q", tc.title, got, tc.want)
			}
		})
	}
}

// Idempotence matters because update_merge_request can be called repeatedly on
// an MR this gate already retitled; a second pass must not stack prefixes.
func TestGitlabDraftTitleIsIdempotent(t *testing.T) {
	first := gitlabDraftTitle("Fix the thing")
	if first != "Draft: Fix the thing" {
		t.Fatalf("first pass = %q", first)
	}
	if second := gitlabDraftTitle(first); second != "" {
		t.Errorf("second pass = %q, want \"\" (no further rewrite)", second)
	}
}
