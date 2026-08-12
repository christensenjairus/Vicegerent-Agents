package main

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
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
	// Edge telemetry, not a credential: it stays so the agent can attribute a
	// delivery, which is what keeps the strip below prefix-scoped.
	request.Header.Set("Cf-Ray", "8f2c1a0e7d3b4c5d-DEN")
	// Credential-shaped headers that must never reach the agent, listed
	// independently of the production code so this test catches provider
	// variants a static denylist missed (GitHub's legacy X-Hub-Signature,
	// Svix's unbranded Webhook-Signature). The Cf-Access- headers are minted by
	// the Cloudflare edge on every tunnelled request and authenticate the caller
	// to Cloudflare, so the agent must not receive them either.
	credentialHeaders := []string{
		"X-PagerDuty-Signature", "X-Hub-Signature-256", "X-Hub-Signature",
		"X-Gitlab-Token", "Svix-Signature", "Webhook-Signature",
		"X-Webhook-Signature-V2", "X-Webhook-Signature", "Authorization",
		"Cf-Access-Jwt-Assertion", "Cf-Access-Authenticated-User-Email",
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
	if received.Header.Get("Content-Type") != "application/json" || received.Header.Get("X-Request-ID") != "delivery-1" || received.Header.Get("Cf-Ray") != "8f2c1a0e7d3b4c5d-DEN" {
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

func TestTimestampedReplayNeverForwards(t *testing.T) {
	for _, test := range []struct {
		provider string
		delivery func() *http.Request
	}{
		{
			provider: "generic-v2",
			delivery: func() *http.Request { return genericV2Delivery("ops", "1700000000") },
		},
		{
			provider: "svix",
			delivery: func() *http.Request { return svixDelivery("ops", "msg_test", "1700000000") },
		},
	} {
		t.Run(test.provider, func(t *testing.T) {
			logs := captureLogs(t)
			forwarded := 0
			upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				forwarded++
				writer.WriteHeader(http.StatusAccepted)
			}))
			defer upstream.Close()

			server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops")
			for attempt, want := range []int{http.StatusAccepted, http.StatusForbidden} {
				response := httptest.NewRecorder()
				server.ServeHTTP(response, test.delivery())
				if response.Code != want {
					t.Fatalf("attempt %d status=%d want=%d", attempt+1, response.Code, want)
				}
			}
			if forwarded != 1 {
				t.Fatalf("replay forwarded %d times", forwarded)
			}
			if output := logs.String(); !strings.Contains(output, "reason=replay_detected") {
				t.Fatalf("replay rejection was not logged: %s", output)
			}
		})
	}
}

func TestRespelledSignatureIsStillAReplay(t *testing.T) {
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()
	server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops")

	// Hex decoding accepts either case, so an uppercased signature verifies. It
	// has to be the delivery that is accepted: a signature rejection and a replay
	// rejection are both a bare 403, so only admitting this one first proves the
	// 403 below came from the replay cache.
	respelled := genericV2Delivery("ops", "1700000000")
	respelled.Header.Set("X-Webhook-Signature-V2", strings.ToUpper(respelled.Header.Get("X-Webhook-Signature-V2")))
	accepted := httptest.NewRecorder()
	server.ServeHTTP(accepted, respelled)
	if accepted.Code != http.StatusAccepted {
		t.Fatalf("uppercased signature status=%d", accepted.Code)
	}

	// The same signature in its original spelling is the same delivery.
	replayed := httptest.NewRecorder()
	server.ServeHTTP(replayed, genericV2Delivery("ops", "1700000000"))
	if replayed.Code != http.StatusForbidden || forwarded != 1 {
		t.Fatalf("respelled replay status=%d forwarded=%d", replayed.Code, forwarded)
	}
}

func TestReplayIsRejectedAcrossTheWholeSignatureWindow(t *testing.T) {
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()
	server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops")

	// One signature verifies from a full tolerance before its timestamp to a full
	// tolerance after it, so accepting at the first instant and replaying at the
	// last is the widest span a recorded key has to survive.
	server.now = func() time.Time { return fixtureNow.Add(-signatureTolerance) }
	accepted := httptest.NewRecorder()
	server.ServeHTTP(accepted, genericV2Delivery("ops", "1700000000"))
	if accepted.Code != http.StatusAccepted {
		t.Fatalf("delivery at the start of the window status=%d", accepted.Code)
	}

	server.now = func() time.Time { return fixtureNow.Add(signatureTolerance) }
	replayed := httptest.NewRecorder()
	server.ServeHTTP(replayed, genericV2Delivery("ops", "1700000000"))
	if replayed.Code != http.StatusForbidden || forwarded != 1 {
		t.Fatalf("replay at the end of the window status=%d forwarded=%d", replayed.Code, forwarded)
	}
}

func TestReplayWindowIsPerAgent(t *testing.T) {
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()
	server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops", "sre")

	// Both agents share a route name and a signing secret, so one signature is
	// valid for both. Neither delivery may consume the other agent's window.
	for _, agent := range []string{"ops", "sre"} {
		response := httptest.NewRecorder()
		server.ServeHTTP(response, genericV2Delivery(agent, "1700000000"))
		if response.Code != http.StatusAccepted {
			t.Fatalf("agent %s status=%d", agent, response.Code)
		}
	}
	if forwarded != 2 {
		t.Fatalf("two agents forwarded %d deliveries", forwarded)
	}
}

func TestRetryAfterAFailedForwardIsNotAReplay(t *testing.T) {
	logs := captureLogs(t)
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()

	rewrite := rewriteTransport(t, upstream.URL)
	// A refused connection to the forward proxy is the failure that proves the
	// request never left this process. It has to come from a real transport: the
	// proxied dial error the listener actually sees is wrapped, so a hand-built
	// dial error would pass this test while production never released a key.
	unreachable := unreachableProxyTransport(t)
	attempts := 0
	transport := roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		attempts++
		if attempts == 1 {
			return unreachable.RoundTrip(request)
		}
		return rewrite.RoundTrip(request)
	})
	server := replayFixture(t, transport, "ops")

	failed := httptest.NewRecorder()
	server.ServeHTTP(failed, genericV2Delivery("ops", "1700000000"))
	if failed.Code != http.StatusBadGateway {
		t.Fatalf("failed forward status=%d", failed.Code)
	}

	// Nothing reached the agent, so the provider redelivering this webhook is a
	// legitimate retry and must still be forwarded.
	retry := httptest.NewRecorder()
	server.ServeHTTP(retry, genericV2Delivery("ops", "1700000000"))
	if retry.Code != http.StatusAccepted || forwarded != 1 {
		t.Fatalf("retry status=%d forwarded=%d", retry.Code, forwarded)
	}
	output := logs.String()
	if strings.Contains(output, "reason=replay_detected") {
		t.Fatalf("retry was rejected as a replay: %s", output)
	}
	if !strings.Contains(output, "reason=upstream_unreachable") {
		t.Fatalf("released delivery was not logged distinctly: %s", output)
	}
}

func TestRetryAfterAnAmbiguousForwardIsAReplay(t *testing.T) {
	logs := captureLogs(t)
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()

	rewrite := rewriteTransport(t, upstream.URL)
	// The connection is established and the request is written before the peer
	// disappears, so the agent may already have acted on this delivery even
	// though no response came back.
	ambiguous := severedProxyTransport(t)
	attempts := 0
	transport := roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		attempts++
		if attempts == 1 {
			return ambiguous.RoundTrip(request)
		}
		return rewrite.RoundTrip(request)
	})
	server := replayFixture(t, transport, "ops")

	failed := httptest.NewRecorder()
	server.ServeHTTP(failed, genericV2Delivery("ops", "1700000000"))
	if failed.Code != http.StatusBadGateway {
		t.Fatalf("ambiguous forward status=%d", failed.Code)
	}

	// Redelivering it could double-act on a webhook the agent already handled,
	// so the claimed key stands and the retry is refused.
	retry := httptest.NewRecorder()
	server.ServeHTTP(retry, genericV2Delivery("ops", "1700000000"))
	if retry.Code != http.StatusForbidden || forwarded != 0 {
		t.Fatalf("retry status=%d forwarded=%d", retry.Code, forwarded)
	}
	if output := logs.String(); !strings.Contains(output, "reason=upstream_unavailable") || !strings.Contains(output, "reason=replay_detected") {
		t.Fatalf("ambiguous forward was not logged distinctly: %s", output)
	}
}

func TestProxyBadGatewayIsNeitherRelayedNorCountedAsADelivery(t *testing.T) {
	logs := captureLogs(t)
	attempts := 0
	// The dedicated proxy answers its own transport failures with a 502 of its
	// own making, so the listener sees a complete response rather than an error.
	badGateway := badGatewayProxyTransport(t)
	transport := roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		attempts++
		return badGateway.RoundTrip(request)
	})
	server := replayFixture(t, transport, "ops")

	failed := httptest.NewRecorder()
	server.ServeHTTP(failed, genericV2Delivery("ops", "1700000000"))
	if failed.Code != http.StatusBadGateway || attempts != 1 {
		t.Fatalf("proxy 502 status=%d attempts=%d", failed.Code, attempts)
	}

	// The proxy's error page names the proxy and the in-cluster address it could
	// not reach, so the provider is answered with the listener's own body.
	if body := failed.Body.String(); !strings.Contains(body, "upstream unavailable") ||
		strings.Contains(body, "mitmproxy") || strings.Contains(body, "agent-sandbox") {
		t.Fatalf("proxy error page relayed to the provider: %q", body)
	}
	if identity := failed.Header().Get("Server"); identity != "" {
		t.Fatalf("proxy identified itself to the provider: %q", identity)
	}
	if output := logs.String(); !strings.Contains(output, "outcome=forward_failed") ||
		!strings.Contains(output, "reason=upstream_bad_gateway") {
		t.Fatalf("proxy 502 was not logged as a failed delivery: %s", output)
	}

	// A 502 cannot prove the delivery never reached the agent: the proxy renders
	// a connection it could not open and a peer that disconnected after reading
	// the request identically. The key stays claimed and the retry is refused.
	retry := httptest.NewRecorder()
	server.ServeHTTP(retry, genericV2Delivery("ops", "1700000000"))
	if retry.Code != http.StatusForbidden || attempts != 1 {
		t.Fatalf("retry status=%d attempts=%d", retry.Code, attempts)
	}
	if output := logs.String(); !strings.Contains(output, "reason=replay_detected") {
		t.Fatalf("retry after a proxy 502 was not refused as a replay: %s", output)
	}
}

func TestAgentFailureStatusOtherThanBadGatewayIsRelayed(t *testing.T) {
	logs := captureLogs(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(writer, "handler exploded")
	}))
	defer upstream.Close()
	server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops")

	// Only the proxy's 502 is reclassified. Every other status is the agent's own
	// answer and reaches the provider unchanged, so the provider retries on the
	// agent's terms rather than the listener's reading of them.
	response := httptest.NewRecorder()
	server.ServeHTTP(response, genericV2Delivery("ops", "1700000000"))
	if response.Code != http.StatusInternalServerError || response.Body.String() != "handler exploded" {
		t.Fatalf("agent 500 status=%d body=%q", response.Code, response.Body.String())
	}
	if output := logs.String(); !strings.Contains(output, "outcome=forwarded") || !strings.Contains(output, "status=500") {
		t.Fatalf("agent 500 was not logged as a delivery: %s", output)
	}
}

func TestSaturatedReplayCacheFailsClosedAsUnavailable(t *testing.T) {
	logs := captureLogs(t)
	forwarded := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		forwarded++
		writer.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()
	server := replayFixture(t, rewriteTransport(t, upstream.URL), "ops")

	for index := range maxReplayCacheKeys {
		server.replays.keys["filler-"+strconv.Itoa(index)] = fixtureNow.Add(signatureTolerance)
	}
	response := httptest.NewRecorder()
	server.ServeHTTP(response, genericV2Delivery("ops", "1700000000"))

	// A saturated cache is the listener's own limit rather than a bad delivery,
	// so it fails closed with a status the provider retries and is never
	// reported as an attacker replay.
	if response.Code != http.StatusServiceUnavailable || forwarded != 0 {
		t.Fatalf("saturated cache status=%d forwarded=%d", response.Code, forwarded)
	}
	if output := logs.String(); !strings.Contains(output, "status=503") || !strings.Contains(output, "reason=replay_cache_full") {
		t.Fatalf("cache saturation was not logged distinctly: %s", output)
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

// replayFixture builds a listener with one route per timestamped provider for
// each named agent, all sharing a single signing secret so a signature accepted
// for one agent is equally valid for the others.
func replayFixture(t *testing.T, transport http.RoundTripper, agents ...string) *webhookServer {
	t.Helper()
	secretRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(secretRoot, "shared"), fixtureSecret, 0o600); err != nil {
		t.Fatal(err)
	}
	var config Config
	for _, agent := range agents {
		for _, provider := range []string{"generic-v2", "svix"} {
			config.Routes = append(config.Routes, Route{
				Agent:      agent,
				Route:      provider,
				Provider:   provider,
				SecretFile: "shared",
				TargetURL:  "http://" + agent + "-webhook.agent-sandbox.svc.cluster.local:8644/webhooks/" + provider,
			})
		}
	}
	server, err := newWebhookServer(config, secretRoot, transport)
	if err != nil {
		t.Fatal(err)
	}
	server.now = func() time.Time { return fixtureNow }
	return server
}

func genericV2Delivery(agent, timestamp string) *http.Request {
	request := httptest.NewRequest(http.MethodPost, "/webhooks/"+agent+"/generic-v2", bytes.NewReader(fixtureBody))
	request.Header.Set("X-Webhook-Timestamp", timestamp)
	request.Header.Set("X-Webhook-Signature-V2", hex.EncodeToString(hmacSHA256(fixtureSecret, append([]byte(timestamp+"."), fixtureBody...))))
	return request
}

func svixDelivery(agent, messageID, timestamp string) *http.Request {
	request := httptest.NewRequest(http.MethodPost, "/webhooks/"+agent+"/svix", bytes.NewReader(fixtureBody))
	request.Header.Set("Svix-Id", messageID)
	request.Header.Set("Svix-Timestamp", timestamp)
	signed := append([]byte(messageID+"."+timestamp+"."), fixtureBody...)
	request.Header.Set("Svix-Signature", "v1,"+base64.StdEncoding.EncodeToString(hmacSHA256(fixtureSecret, signed)))
	return request
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

// unreachableProxyTransport forwards through the listener's real proxy transport
// pointed at a closed port, reproducing the wrapped dial error a refused or
// unresolvable webhook-egress-proxy produces in the cluster.
func unreachableProxyTransport(t *testing.T) http.RoundTripper {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	transport, err := newForwardTransport("http://" + address)
	if err != nil {
		t.Fatal(err)
	}
	return transport
}

// severedProxyTransport forwards through the real proxy transport to a peer that
// accepts the connection, reads the delivery, and disconnects without answering.
func severedProxyTransport(t *testing.T) http.RoundTripper {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			_, _ = connection.Read(make([]byte, 4096))
			_ = connection.Close()
		}
	}()
	transport, err := newForwardTransport("http://" + listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	return transport
}

// badGatewayProxyTransport forwards through the real proxy transport to a peer
// that answers every delivery with the response mitmproxy renders when it cannot
// complete a request, error page and all. Nothing in it distinguishes a
// connection the proxy never opened from one it lost mid-request, which is why
// the listener cannot release a replay key on the strength of a 502.
func badGatewayProxyTransport(t *testing.T) http.RoundTripper {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })

	page := strings.Join([]string{
		"<html>", "<head>", "    <title>502 Bad Gateway</title>", "</head>", "<body>",
		"    <h1>502 Bad Gateway</h1>",
		"    <p>Cannot connect to ops-webhook.agent-sandbox.svc.cluster.local:8644: [Errno 111] Connection refused</p>",
		"</body>", "</html>",
	}, "\n")
	answer := "HTTP/1.1 502 Bad Gateway\r\nServer: mitmproxy 12.2.3\r\nConnection: close\r\n" +
		"Content-Type: text/html\r\nContent-Length: " + strconv.Itoa(len(page)) + "\r\n\r\n" + page
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			_, _ = connection.Read(make([]byte, 4096))
			_, _ = io.WriteString(connection, answer)
			_ = connection.Close()
		}
	}()

	transport, err := newForwardTransport("http://" + listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	return transport
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
