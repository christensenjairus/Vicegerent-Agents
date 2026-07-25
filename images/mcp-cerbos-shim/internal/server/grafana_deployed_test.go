package server

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"gopkg.in/yaml.v3"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("grafana_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns a Grafana
// tool call into the grafana_datasource resource Cerbos denies for OpenSearch;
// the deny *decision* itself is proven by defs/grafana_test.yaml.

// deployedResult is the cached outcome of rendering the shim's mapping ConfigMap
// from charts/mcp-cerbos-shim. Exactly one of {mapping, skip, err} is set.
type deployedResult struct {
	mapping *config.Mapping
	skip    string
	err     error
}

// loadDeployedMapping renders the chart once for the whole package (~71 callers).
// The mapping now lives at charts/mcp-cerbos-shim/files/mapping.yaml as a Helm
// template ({{ ... }}), so it is only loadable after rendering — we mirror
// scripts/validate.sh's invocation and read the ConfigMap's mapping.yaml key.
var loadDeployedMapping = sync.OnceValue(renderDeployedMapping)

func renderDeployedMapping() deployedResult {
	// Skip (not fail) only where Helm genuinely can't run: a machine without
	// helm, or a bare checkout missing the chart. A render/parse error is a real
	// failure and must surface loudly.
	if _, err := exec.LookPath("helm"); err != nil {
		return deployedResult{skip: "helm not on PATH"}
	}
	root, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		return deployedResult{err: fmt.Errorf("resolve repo root: %w", err)}
	}
	chart := filepath.Join(root, "charts", "mcp-cerbos-shim")
	if _, err := os.Stat(chart); err != nil {
		return deployedResult{skip: fmt.Sprintf("chart %s not present: %v", chart, err)}
	}

	var stdout, stderr bytes.Buffer
	cmd := exec.Command("helm", "template", "mcp-cerbos-shim", chart,
		"-f", filepath.Join(root, "values.defaults.yaml"),
		"-f", filepath.Join(root, "values.example.yaml"),
		"--show-only", "templates/configmap.yaml")
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return deployedResult{err: fmt.Errorf("helm template mcp-cerbos-shim failed: %v\n%s%s", err, stdout.String(), stderr.String())}
	}

	var cm struct {
		Data map[string]string `yaml:"data"`
	}
	if err := yaml.Unmarshal(stdout.Bytes(), &cm); err != nil {
		return deployedResult{err: fmt.Errorf("parse rendered configmap: %w", err)}
	}
	text, ok := cm.Data["mapping.yaml"]
	if !ok {
		return deployedResult{err: fmt.Errorf("rendered configmap has no data key mapping.yaml")}
	}

	dir, err := os.MkdirTemp("", "deployed-mapping-")
	if err != nil {
		return deployedResult{err: fmt.Errorf("tempdir: %w", err)}
	}
	defer os.RemoveAll(dir)
	p := filepath.Join(dir, "mapping.yaml")
	if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
		return deployedResult{err: fmt.Errorf("write rendered mapping: %w", err)}
	}
	m, err := config.Load(p)
	if err != nil {
		return deployedResult{err: fmt.Errorf("load rendered mapping: %w", err)}
	}
	return deployedResult{mapping: m}
}

func deployedMapping(t *testing.T) *config.Mapping {
	t.Helper()
	r := loadDeployedMapping()
	switch {
	case r.skip != "":
		t.Skip(r.skip)
	case r.err != nil:
		t.Fatalf("%v", r.err)
	}
	return r.mapping
}

func TestDeployedGrafanaMapping_OpenSearchReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	const osUID = "fess5o6x6evb4b"
	const osName = "dev-opensearch-datasource"

	cases := []struct {
		tool     string
		args     map[string]any
		wantUID  string
		wantName string
	}{
		{"grafana_query_prometheus", map[string]any{"datasourceUid": osUID, "expr": "up"}, osUID, ""},
		{"grafana_query_prometheus_histogram", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_list_prometheus_label_names", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_list_prometheus_metric_names", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_get_datasource", map[string]any{"uid": osUID}, osUID, ""},
		{"grafana_get_datasource", map[string]any{"name": osName}, "", osName},
		// Loki/VictoriaLogs read tools: same datasourceUid arg shape as the
		// Prometheus tools above, must reach Cerbos as grafana_datasource too.
		{"grafana_query_loki_logs", map[string]any{"datasourceUid": osUID, "logql": "{app=\"x\"}"}, osUID, ""},
		{"grafana_query_loki_stats", map[string]any{"datasourceUid": osUID, "logql": "{app=\"x\"}"}, osUID, ""},
		{"grafana_list_loki_label_names", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_list_loki_label_values", map[string]any{"datasourceUid": osUID, "labelName": "app"}, osUID, ""},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			// allow=false: the shim must forward a well-formed resource to Cerbos
			// and honor its deny (turning it into a PERMISSION_DENIED error).
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies, got pass")
			}
			assertNoSideEffects(t, res)
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "grafana_datasource" {
				t.Errorf("resourceType = %q, want grafana_datasource", d.gotType)
			}
			if d.gotAct != "read" {
				t.Errorf("action = %q, want read", d.gotAct)
			}
			if d.gotAttr["uid"] != tc.wantUID {
				t.Errorf("attr.uid = %q, want %q", d.gotAttr["uid"], tc.wantUID)
			}
			if d.gotAttr["name"] != tc.wantName {
				t.Errorf("attr.name = %q, want %q", d.gotAttr["name"], tc.wantName)
			}
		})
	}
}

func TestDeployedGrafanaMapping_NonOpenSearchPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A prometheus datasource uid: mapped, reaches Cerbos, allowed.
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("grafana_query_prometheus",
			map[string]any{"datasourceUid": "prom-abc123", "expr": "up"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for a non-opensearch datasource")
	}
	if d.gotAttr["uid"] != "prom-abc123" {
		t.Errorf("attr.uid = %q, want prom-abc123", d.gotAttr["uid"])
	}
}

func TestDeployedGrafanaMapping_UnmappedGrafanaToolPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A scoped read tool that names no datasource is unmapped -> passes without
	// a Cerbos call. Confirms the guardrail doesn't over-block the allowed tools.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("grafana_search_dashboards",
			map[string]any{"query": "prod"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an unmapped grafana tool")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}

func TestDeployedGrafanaMapping_CheckDatasourcesHealthPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// check_datasources_health takes a plural `uids` array — the single-
	// resource-per-call model can't check "any of these is OpenSearch", and
	// it only reveals up/down status rather than a datasource's actual data,
	// so it's deliberately unmapped rather than mapped-but-wrong.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("grafana_check_datasources_health",
			map[string]any{"uids": []any{"fess5o6x6evb4b"}})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for unmapped check_datasources_health")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}
