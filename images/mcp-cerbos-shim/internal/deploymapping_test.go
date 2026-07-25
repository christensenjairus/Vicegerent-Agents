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

	// The deployed mapping is a Helm template (charts/mcp-cerbos-shim/files/
	// mapping.yaml), so render it through the chart before loading — mirroring
	// scripts/validate.sh. Skip only when helm is unavailable or the chart is
	// missing (bare checkout); a render/parse failure is a real error.
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

	var stdout, stderr bytes.Buffer
	cmd := exec.Command("helm", "template", "mcp-cerbos-shim", chart,
		"-f", filepath.Join(root, "values.defaults.yaml"),
		"-f", filepath.Join(root, "values.example.yaml"),
		"--show-only", "templates/configmap.yaml")
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

	p := filepath.Join(t.TempDir(), "mapping.yaml")
	if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
		t.Fatalf("write rendered mapping: %v", err)
	}
	if _, err := Load(p); err != nil {
		t.Errorf("rendered chart mapping failed to parse/validate: %v", err)
	}
}
