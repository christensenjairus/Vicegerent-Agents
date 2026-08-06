package config

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestShippedMappingsParse ensures every mapping YAML we ship (the example and
// the deploy ConfigMap source) still parses + structurally validates after
// yamlfix reformatting. Guards against a formatter breaking runtime config.
func TestShippedMappingsParse(t *testing.T) {
	// The committed example is raw YAML, loadable directly.
	example := filepath.Join("..", "mapping.example.yaml")
	if _, err := os.Stat(example); err == nil {
		if _, err := Load(example); err != nil {
			t.Errorf("%s failed to parse/validate: %v", example, err)
		}
	}

	// Rendering with the example machine values is the common case; rendering
	// with the defaults alone is the fresh-machine case, where every clusterVar
	// is at its empty default. Both must produce a loadable mapping.
	for _, machine := range []string{"values.example.yaml", ""} {
		name := machine
		if name == "" {
			name = "defaults-only"
		}
		t.Run(name, func(t *testing.T) {
			p := filepath.Join(t.TempDir(), "mapping.yaml")
			if err := os.WriteFile(p, []byte(renderDeployedMapping(t, machine)), 0o600); err != nil {
				t.Fatalf("write rendered mapping: %v", err)
			}
			if _, err := Load(p); err != nil {
				t.Errorf("rendered chart mapping failed to parse/validate: %v", err)
			}
		})
	}
}

// TestDeployedMappingForcedCreatedBy pins the one Helm-substituted value in the
// deployed mapping: alertmanager createSilence force-stamps this machine's
// ${alertmanagerCreatedBy} so deleteSilence's live ownership lookup has
// something real to verify against. On a machine that never set it the force
// block must be absent entirely — an unquoted empty value would render a YAML
// null and stamp createdBy: nil onto every silence, which no later ownership
// check could match.
func TestDeployedMappingForcedCreatedBy(t *testing.T) {
	for _, tc := range []struct {
		machine string
		want    any // nil means the force block must not be present
	}{
		{machine: "values.example.yaml", want: "your-username"},
		{machine: "", want: nil},
	} {
		name := tc.machine
		if name == "" {
			name = "defaults-only"
		}
		t.Run(name, func(t *testing.T) {
			var m Mapping
			if err := yaml.Unmarshal([]byte(renderDeployedMapping(t, tc.machine)), &m); err != nil {
				t.Fatalf("parse rendered mapping: %v", err)
			}
			for _, tool := range []string{"alertmanager_createSilence", "alertmanager_secondary_createSilence"} {
				got, ok := m.Backends["vmcp"].Tools[tool]
				if !ok {
					t.Fatalf("%s missing from the rendered mapping", tool)
				}
				if tc.want == nil {
					if _, forced := got.Force["createdBy"]; forced {
						t.Errorf("%s: force.createdBy = %#v, want no force block", tool, got.Force["createdBy"])
					}
					continue
				}
				if got.Force["createdBy"] != tc.want {
					t.Errorf("%s: force.createdBy = %#v, want %#v", tool, got.Force["createdBy"], tc.want)
				}
			}
		})
	}
}

// renderDeployedMapping renders charts/mcp-cerbos-shim's ConfigMap through helm
// — mirroring scripts/validate.sh's layering — and returns the mapping.yaml it
// carries. machineValues is layered over values.defaults.yaml, or "" for a
// defaults-only render. Skips only when helm is unavailable or the chart is
// missing (bare checkout); a render/parse failure is a real error.
func renderDeployedMapping(t *testing.T, machineValues string) string {
	t.Helper()
	if _, err := exec.LookPath("helm"); err != nil {
		t.Skip("helm not on PATH; only mapping.example.yaml checked")
	}
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("resolve repo root: %v", err)
	}
	chart := filepath.Join(root, "charts", "mcp-cerbos-shim")
	if _, err := os.Stat(chart); err != nil {
		t.Skipf("chart %s not present: %v", chart, err)
	}

	args := []string{"template", "mcp-cerbos-shim", chart,
		"-f", filepath.Join(root, "values.defaults.yaml")}
	if machineValues != "" {
		args = append(args, "-f", filepath.Join(root, machineValues))
	}
	args = append(args, "--show-only", "templates/configmap.yaml")

	var stdout, stderr bytes.Buffer
	cmd := exec.Command("helm", args...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("helm template mcp-cerbos-shim failed: %v\n%s%s", err, stdout.String(), stderr.String())
	}

	var cm struct {
		Data map[string]string `yaml:"data"`
	}
	if err := yaml.Unmarshal(stdout.Bytes(), &cm); err != nil {
		t.Fatalf("parse rendered configmap: %v", err)
	}
	text, ok := cm.Data["mapping.yaml"]
	if !ok {
		t.Fatalf("rendered configmap has no data key mapping.yaml")
	}
	return text
}
