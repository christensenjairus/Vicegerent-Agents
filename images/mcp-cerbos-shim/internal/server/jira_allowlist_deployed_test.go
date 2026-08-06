package server

import (
	"sort"
	"testing"
)

// TestJiraAllowlist_EveryToolIsMapped keeps a defaultAction:allow vMCP backend
// from exposing an unreviewed Jira tool. Jira's selected surface must consist
// only of tools whose request arguments the shim maps to Cerbos resources.
func TestJiraAllowlist_EveryToolIsMapped(t *testing.T) {
	m := deployedMapping(t)
	backend, ok := m.Backends["vmcp"]
	if !ok {
		t.Fatal("rendered mapping has no vmcp backend")
	}

	var unmapped []string
	for tool := range loadToolAllowlist(t, "jira") {
		if _, mapped := backend.Tools[tool]; !mapped {
			unmapped = append(unmapped, tool)
		}
	}
	sort.Strings(unmapped)
	if len(unmapped) > 0 {
		t.Fatalf("Jira tools are allowlisted but unmapped (and therefore ungated on a defaultAction:allow backend): %v", unmapped)
	}
}
