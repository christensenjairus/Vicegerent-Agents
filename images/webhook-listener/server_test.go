package main

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProxyAuthenticatesAndStripsSignature(t *testing.T) {
	logs := captureLogs(t)
	var received *http.Request
	var receivedBody []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		received = request.Clone(request.Context())
		receivedBody, _ = io.ReadAll(request.Body)
		writer.Header().Set("X-Upstream", "ok")
		writer.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(writer, "accepted")
	}))
	defer upstream.Close()

	secretRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(secretRoot, "pagerduty-production"), fixtureSecret, 0o600); err != nil {
		t.Fatal(err)
	}
	transport := rewriteTransport(t, upstream.URL)
	server, err := newWebhookServer(fixtureConfig(), secretRoot, transport)
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodPost, "/webhooks/ops/pagerduty-incidents?attempt=1", bytes.NewReader(fixtureBody))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-PagerDuty-Signature", "v1=6220f3fc1b90181b62bfd612f290bdf368b6b66b47ab91130794ad1a947e46af")
	request.Header.Set("X-Request-ID", "delivery-1")
	// Credential-shaped headers that must never reach the agent, listed
	// independently of the production code so this test catches provider
	// variants a static denylist missed (GitHub's legacy X-Hub-Signature,
	// Svix's unbranded Webhook-Signature).
	credentialHeaders := []string{
		"X-PagerDuty-Signature", "X-Hub-Signature-256", "X-Hub-Signature",
		"X-Gitlab-Token", "Svix-Signature", "Webhook-Signature",
		"X-Webhook-Signature-V2", "X-Webhook-Signature", "Authorization",
	}
	for _, header := range credentialHeaders {
		if header != "X-PagerDuty-Signature" {
			request.Header.Set(header, "must-not-leak")
		}
	}
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)

	if response.Code != http.StatusAccepted || response.Body.String() != "accepted" {
		t.Fatalf("unexpected response: %d %q", response.Code, response.Body.String())
	}
	if received == nil || received.URL.Path != "/webhooks/pagerduty-incidents" || received.URL.RawQuery != "attempt=1" {
		t.Fatalf("unexpected upstream request: %#v", received)
	}
	if !bytes.Equal(receivedBody, fixtureBody) {
		t.Fatalf("body changed: %q", receivedBody)
	}
	if received.Header.Get("Content-Type") != "application/json" || received.Header.Get("X-Request-ID") != "delivery-1" {
		t.Fatalf("ordinary headers were not preserved: %#v", received.Header)
	}
	for _, header := range credentialHeaders {
		if value := received.Header.Get(header); value != "" {
			t.Fatalf("signature header %s leaked upstream: %q", header, value)
		}
	}
	if response.Header().Get("X-Upstream") != "ok" {
		t.Fatal("upstream response headers were not preserved")
	}
	output := logs.String()
	if !strings.Contains(output, "outcome=forwarded agent=ops route=pagerduty-incidents provider=pagerduty status=202") {
		t.Fatalf("forwarded delivery was not logged: %s", output)
	}
	if strings.Contains(output, "delivery-1") || strings.Contains(output, "6220f3fc") || strings.Contains(output, string(fixtureBody)) {
		t.Fatalf("delivery log included request metadata or body: %s", output)
	}
}

func TestForwardTransportUsesConfiguredProxy(t *testing.T) {
	called := false
	proxy := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		called = true
		if request.URL.Host != "ops-webhook.agent-sandbox.svc.cluster.local:8644" {
			t.Fatalf("proxy received unexpected target URL: %s", request.URL.String())
		}
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer proxy.Close()

	transport, err := newForwardTransport(proxy.URL)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: transport}
	request, err := http.NewRequest(http.MethodPost, "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/incidents", nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()

	if !called || response.StatusCode != http.StatusAccepted {
		t.Fatalf("agent request bypassed configured proxy: called=%t status=%d", called, response.StatusCode)
	}
}

func TestForwardTransportRejectsAnythingButHTTPOrigin(t *testing.T) {
	for _, value := range []string{"", "https://proxy.example", "http://user@proxy.example", "http://proxy.example/path", "http://proxy.example?query=yes"} {
		if _, err := newForwardTransport(value); err == nil {
			t.Errorf("invalid proxy URL %q was accepted", value)
		}
	}
}

func TestUnknownAndDisabledRoutesAreIndistinguishable(t *testing.T) {
	secretRoot := t.TempDir()
	server, err := newWebhookServer(fixtureConfig(), secretRoot, http.DefaultTransport)
	if err != nil {
		t.Fatal(err)
	}
	paths := []string{
		"/webhooks/unknown/pagerduty-incidents",
		"/webhooks/ops/unknown",
		"/webhooks/ops/disabled-route",
	}
	var first string
	for _, path := range paths {
		response := httptest.NewRecorder()
		server.ServeHTTP(response, httptest.NewRequest(http.MethodPost, path, nil))
		if response.Code != http.StatusNotFound {
			t.Fatalf("%s returned %d", path, response.Code)
		}
		if first == "" {
			first = response.Body.String()
		} else if response.Body.String() != first {
			t.Fatalf("%s returned a distinguishable body %q", path, response.Body.String())
		}
	}
}

func TestRejectedSignatureNeverForwards(t *testing.T) {
	logs := captureLogs(t)
	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer upstream.Close()

	secretRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(secretRoot, "pagerduty-production"), fixtureSecret, 0o600); err != nil {
		t.Fatal(err)
	}
	server, err := newWebhookServer(fixtureConfig(), secretRoot, rewriteTransport(t, upstream.URL))
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/webhooks/ops/pagerduty-incidents", bytes.NewReader(fixtureBody))
	request.Header.Set("X-PagerDuty-Signature", "v1=deadbeef")
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusForbidden || called {
		t.Fatalf("rejected request status=%d forwarded=%t", response.Code, called)
	}
	if output := logs.String(); !strings.Contains(output, "outcome=rejected agent=ops route=pagerduty-incidents provider=pagerduty status=403") || !strings.Contains(output, "reason=signature_invalid") {
		t.Fatalf("rejected delivery was not logged: %s", output)
	}
}

func TestOversizedPayloadIsRejectedBeforeForwarding(t *testing.T) {
	logs := captureLogs(t)
	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer upstream.Close()

	secretRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(secretRoot, "pagerduty-production"), fixtureSecret, 0o600); err != nil {
		t.Fatal(err)
	}
	server, err := newWebhookServer(fixtureConfig(), secretRoot, rewriteTransport(t, upstream.URL))
	if err != nil {
		t.Fatal(err)
	}
	oversized := bytes.Repeat([]byte("a"), maxBodyBytes+1)
	request := httptest.NewRequest(http.MethodPost, "/webhooks/ops/pagerduty-incidents", bytes.NewReader(oversized))
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusRequestEntityTooLarge || called {
		t.Fatalf("oversized payload status=%d forwarded=%t", response.Code, called)
	}
	if output := logs.String(); !strings.Contains(output, "status=413") || !strings.Contains(output, "reason=payload_too_large") {
		t.Fatalf("oversized payload was not logged: %s", output)
	}
}

func TestEmptySigningSecretFailsClosed(t *testing.T) {
	logs := captureLogs(t)
	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer upstream.Close()

	secretRoot := t.TempDir()
	// An empty secret file must never authenticate a delivery.
	if err := os.WriteFile(filepath.Join(secretRoot, "pagerduty-production"), []byte{}, 0o600); err != nil {
		t.Fatal(err)
	}
	server, err := newWebhookServer(fixtureConfig(), secretRoot, rewriteTransport(t, upstream.URL))
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/webhooks/ops/pagerduty-incidents", bytes.NewReader(fixtureBody))
	request.Header.Set("X-PagerDuty-Signature", "v1=6220f3fc1b90181b62bfd612f290bdf368b6b66b47ab91130794ad1a947e46af")
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable || called {
		t.Fatalf("empty secret status=%d forwarded=%t", response.Code, called)
	}
	if output := logs.String(); !strings.Contains(output, "status=503") || !strings.Contains(output, "reason=signing_secret_unavailable") {
		t.Fatalf("empty secret was not logged: %s", output)
	}
}

func TestHealthzDoesNotRequireARouteOrSecret(t *testing.T) {
	server, err := newWebhookServer(fixtureConfig(), t.TempDir(), http.DefaultTransport)
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	server.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("healthz returned %d", response.Code)
	}
}

func TestCompileRoutesRejectsCrossAgentTarget(t *testing.T) {
	config := fixtureConfig()
	config.Routes[0].TargetURL = "http://other-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/pagerduty-incidents"
	if _, err := compileRoutes(config); err == nil || !strings.Contains(err.Error(), "must be exactly") {
		t.Fatalf("cross-agent target was not rejected: %v", err)
	}
}

func TestCompileRoutesAcceptsDerivedSecretFile(t *testing.T) {
	config := fixtureConfig()
	config.Routes[0].SecretFile = "bot-jchristensen__test" // pragma: allowlist secret
	if _, err := compileRoutes(config); err != nil {
		t.Fatalf("derived Secret filename was rejected: %v", err)
	}
}

func TestAlertmanagerCredentialIsStrippedBeforeTheAgent(t *testing.T) {
	logs := captureLogs(t)
	var received *http.Request
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		received = request.Clone(request.Context())
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()

	secretRoot := t.TempDir()
	config := Config{Routes: []Route{
		{
			Agent:      "ops",
			Route:      "alertmanager-alerts",
			Provider:   "alertmanager",
			SecretFile: "ops__alertmanager-alerts",
			TargetURL:  "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/alertmanager-alerts",
		},
	}}
	if err := os.WriteFile(filepath.Join(secretRoot, "ops__alertmanager-alerts"), fixtureSecret, 0o600); err != nil {
		t.Fatal(err)
	}
	server, err := newWebhookServer(config, secretRoot, rewriteTransport(t, upstream.URL))
	if err != nil {
		t.Fatal(err)
	}

	alertBody := []byte(`{"status":"firing","commonLabels":{"alertname":"TargetDown"}}`)
	request := httptest.NewRequest(http.MethodPost, "/webhooks/ops/alertmanager-alerts", bytes.NewReader(alertBody))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+string(fixtureSecret))
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)

	if response.Code != http.StatusAccepted {
		t.Fatalf("valid Alertmanager delivery returned %d", response.Code)
	}
	if received == nil || received.Header.Get("Authorization") != "" {
		t.Fatalf("Alertmanager credential leaked into the agent sandbox: %#v", received)
	}
	if output := logs.String(); !strings.Contains(output, "provider=alertmanager status=202") {
		t.Fatalf("delivery was not logged: %s", output)
	}
	if strings.Contains(logs.String(), string(fixtureSecret)) {
		t.Fatal("credential was logged")
	}

	// An unauthenticated delivery must be rejected before any forwarding.
	received = nil
	rejected := httptest.NewRequest(http.MethodPost, "/webhooks/ops/alertmanager-alerts", bytes.NewReader(alertBody))
	unauthorized := httptest.NewRecorder()
	server.ServeHTTP(unauthorized, rejected)
	if unauthorized.Code != http.StatusForbidden || received != nil {
		t.Fatalf("unauthenticated Alertmanager delivery reached the agent: %d", unauthorized.Code)
	}
}

func fixtureConfig() Config {
	return Config{Routes: []Route{
		{
			Agent:      "ops",
			Route:      "pagerduty-incidents",
			Provider:   "pagerduty",
			SecretFile: "pagerduty-production",
			TargetURL:  "http://ops-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/pagerduty-incidents",
		},
	}}
}

func rewriteTransport(t *testing.T, destination string) http.RoundTripper {
	t.Helper()
	target, err := url.Parse(destination)
	if err != nil {
		t.Fatal(err)
	}
	return roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		cloned := request.Clone(request.Context())
		cloned.URL.Scheme = target.Scheme
		cloned.URL.Host = target.Host
		return http.DefaultTransport.RoundTrip(cloned)
	})
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (function roundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var output bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(previous) })
	return &output
}
