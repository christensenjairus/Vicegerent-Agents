package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	maxBodyBytes       = 1 << 20
	maxReplayCacheKeys = 10_000
)

var hopByHopHeaders = []string{
	"Connection",
	"Proxy-Connection",
	"Keep-Alive",
	"Proxy-Authenticate",
	"Proxy-Authorization",
	"Te",
	"Trailer",
	"Transfer-Encoding",
	"Upgrade",
}

type webhookServer struct {
	routes     map[string]compiledRoute
	secretRoot string
	client     *http.Client
	now        func() time.Time
	replays    replayCache
}

type replayCache struct {
	mu   sync.Mutex
	keys map[string]time.Time
}

func newWebhookServer(config Config, secretRoot string, transport http.RoundTripper) (*webhookServer, error) {
	routes, err := compileRoutes(config)
	if err != nil {
		return nil, err
	}
	if !filepath.IsAbs(secretRoot) {
		return nil, fmt.Errorf("secret root must be absolute")
	}
	if transport == nil {
		transport = http.DefaultTransport
	}
	return &webhookServer{
		routes:     routes,
		secretRoot: secretRoot,
		client: &http.Client{
			Transport: transport,
			Timeout:   30 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		now:     time.Now,
		replays: replayCache{keys: make(map[string]time.Time)},
	}, nil
}

func (server *webhookServer) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.Method == http.MethodGet && request.URL.Path == "/healthz" {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, `{"status":"ok"}`+"\n")
		return
	}

	route, found := server.routes[request.URL.Path]
	if request.Method != http.MethodPost || !found {
		writeNotFound(writer)
		return
	}
	started := time.Now()
	if request.ContentLength > maxBodyBytes {
		http.Error(writer, "payload too large", http.StatusRequestEntityTooLarge)
		logDelivery(route, "rejected", http.StatusRequestEntityTooLarge, int(request.ContentLength), started, "payload_too_large")
		return
	}

	request.Body = http.MaxBytesReader(writer, request.Body, maxBodyBytes)
	body, err := io.ReadAll(request.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			http.Error(writer, "payload too large", http.StatusRequestEntityTooLarge)
			logDelivery(route, "rejected", http.StatusRequestEntityTooLarge, maxBodyBytes, started, "payload_too_large")
			return
		}
		http.Error(writer, "bad request", http.StatusBadRequest)
		logDelivery(route, "rejected", http.StatusBadRequest, 0, started, "read_failed")
		return
	}

	secret, err := os.ReadFile(filepath.Join(server.secretRoot, route.SecretFile))
	if err != nil || len(secret) == 0 {
		http.Error(writer, "webhook unavailable", http.StatusServiceUnavailable)
		logDelivery(route, "rejected", http.StatusServiceUnavailable, len(body), started, "signing_secret_unavailable")
		return
	}
	if err := verifySignature(route.Provider, request.Header, body, secret, server.now()); err != nil {
		http.Error(writer, "forbidden", http.StatusForbidden)
		logDelivery(route, "rejected", http.StatusForbidden, len(body), started, "signature_invalid")
		return
	}
	replay := replayKey(route.Agent, route.Provider, route.Route.Route, request.Header)
	if replay != "" {
		if reason := server.replays.record(replay, server.now()); reason != "" {
			status, message := http.StatusForbidden, "forbidden"
			if reason == "replay_cache_full" {
				// Our own capacity limit, not a bad delivery, so answer with a
				// status the provider retries.
				status, message = http.StatusServiceUnavailable, "webhook unavailable"
			}
			http.Error(writer, message, status)
			logDelivery(route, "rejected", status, len(body), started, reason)
			return
		}
	}

	status, headerWritten, err := server.forward(writer, request, body, route)
	if err != nil {
		if headerWritten {
			// The upstream status and headers were already sent, so we can only
			// abandon the truncated response -- not replace it with a 502. The
			// delivery did reach the agent, so its replay key stays claimed.
			logDelivery(route, "forward_truncated", status, len(body), started, "response_copy_failed")
			return
		}
		reason := "upstream_unavailable"
		switch {
		case errors.Is(err, errUpstreamBadGateway):
			reason = "upstream_bad_gateway"
		case neverSent(err):
			// The connection was never established, so the delivery provably did
			// not reach the agent and the provider's redelivery of it is a retry.
			server.replays.forget(replay)
			reason = "upstream_unreachable"
		}
		http.Error(writer, "upstream unavailable", http.StatusBadGateway)
		logDelivery(route, "forward_failed", http.StatusBadGateway, len(body), started, reason)
		return
	}
	logDelivery(route, "forwarded", status, len(body), started, "")
}

// record admits the first delivery for a key and rejects the rest, claiming the
// key before forwarding so a concurrent duplicate cannot reach the agent. It
// returns the empty string when the delivery is admitted, and otherwise the log
// reason for the rejection, so a saturated cache is never reported as a replay.
//
// An entry has to outlive the signature that produced it. A signature verifies
// while its timestamp is within signatureTolerance of now, so the first accepted
// copy can arrive a full tolerance before the last still-valid replay of it:
// twice the tolerance is the shortest lifetime that covers the whole window, and
// the bound is inclusive because a signature verifies at both of its endpoints.
func (cache *replayCache) record(key string, now time.Time) string {
	cache.mu.Lock()
	defer cache.mu.Unlock()

	if expiresAt, seen := cache.keys[key]; seen && !expiresAt.Before(now) {
		return "replay_detected"
	}
	if len(cache.keys) >= maxReplayCacheKeys {
		// Sweep only when the cache would otherwise turn a valid delivery away,
		// so the ordinary path stays a single map lookup.
		for existing, expiresAt := range cache.keys {
			if expiresAt.Before(now) {
				delete(cache.keys, existing)
			}
		}
		if len(cache.keys) >= maxReplayCacheKeys {
			return "replay_cache_full"
		}
	}
	cache.keys[key] = now.Add(2 * signatureTolerance)
	return ""
}

// forget releases a key claimed for a delivery that provably never left this
// process, so the provider's retry of it is not mistaken for a replay.
func (cache *replayCache) forget(key string) {
	if key == "" {
		return
	}
	cache.mu.Lock()
	defer cache.mu.Unlock()
	delete(cache.keys, key)
}

// errUpstreamBadGateway reports that the forward completed but answered 502,
// which the dedicated proxy synthesizes when it cannot complete the request to
// the agent. It never releases the delivery's replay key: the proxy renders a
// connection it could not open and a peer that disconnected after reading the
// request as the same untagged 502, so the status cannot prove the agent did
// not act on the delivery.
var errUpstreamBadGateway = errors.New("upstream answered 502")

// neverSent reports whether a forwarding error happened while establishing the
// connection, which is the only failure that proves the request never left this
// process. A write, a read, or a timeout after that may already have been acted
// on by the agent, so those keep the delivery's replay key claimed and the
// provider's retry is answered with a 403 rather than delivered twice.
//
// Every forward goes through WEBHOOK_FORWARD_PROXY, and the transport reports a
// failed dial to that proxy as an outer net.OpError with Op "proxyconnect"
// wrapping the dial error. errors.As stops at the outermost match, so the chain
// is walked link by link instead: matching only the outer error would classify
// every refused or unresolvable proxy as ambiguous and never release a key.
func neverSent(err error) bool {
	for current := err; current != nil; current = errors.Unwrap(current) {
		opError, isOpError := current.(*net.OpError)
		if isOpError && (opError.Op == "dial" || opError.Op == "proxyconnect") {
			return true
		}
	}
	return false
}

// forward proxies the delivery to the agent. The bool reports whether the
// response header was already written to the client, so the caller knows a
// late error can no longer be turned into a clean 502.
func (server *webhookServer) forward(writer http.ResponseWriter, inbound *http.Request, body []byte, route compiledRoute) (int, bool, error) {
	target := cloneURL(route.target)
	target.RawQuery = inbound.URL.RawQuery
	outbound, err := http.NewRequestWithContext(inbound.Context(), inbound.Method, target.String(), bytes.NewReader(body))
	if err != nil {
		return 0, false, err
	}
	copyHeaders(outbound.Header, inbound.Header)
	removeHopByHopHeaders(outbound.Header)
	stripCredentialHeaders(outbound.Header)

	response, err := server.client.Do(outbound)
	if err != nil {
		return 0, false, err
	}
	defer response.Body.Close()

	if response.StatusCode == http.StatusBadGateway {
		// The dedicated proxy answers 502 for its own transport failures, so this
		// delivery may never have reached the agent and must not be logged as one
		// that did. Its body is not relayed either: the proxy's error page names
		// the proxy and the in-cluster target it could not reach, and the provider
		// gets nothing from either. An agent that answers 502 itself is reported
		// the same way, which is the honest reading of an unusable response.
		return 0, false, errUpstreamBadGateway
	}

	copyHeaders(writer.Header(), response.Header)
	removeHopByHopHeaders(writer.Header())
	writer.WriteHeader(response.StatusCode)
	_, err = io.Copy(writer, response.Body)
	return response.StatusCode, true, err
}

// isCredentialHeader reports whether a header carries authentication material --
// a provider signature, a shared token, an Authorization credential, or a
// Cloudflare Access assertion. Matching by shape rather than an exact denylist
// strips every provider's signing input, current or future (e.g. GitHub's legacy
// X-Hub-Signature and Svix's unbranded Webhook-Signature), before the delivery
// reaches the agent. The whole Cf-Access- prefix goes with them: those headers
// are minted by the edge to authenticate the caller to Cloudflare, and the agent
// must not be able to read or replay them. The remaining Cf- headers are
// delivery telemetry (client IP, ray ID) and are forwarded.
func isCredentialHeader(name string) bool {
	lower := strings.ToLower(name)
	if lower == "authorization" || strings.HasPrefix(lower, "cf-access-") {
		return true
	}
	return strings.Contains(lower, "signature") || strings.Contains(lower, "token")
}

func stripCredentialHeaders(headers http.Header) {
	for name := range headers {
		if isCredentialHeader(name) {
			headers.Del(name)
		}
	}
}

func logDelivery(route compiledRoute, outcome string, status, requestBytes int, started time.Time, reason string) {
	message := fmt.Sprintf(
		"webhook outcome=%s agent=%s route=%s provider=%s status=%d request_bytes=%d duration_ms=%d",
		outcome, route.Agent, route.Route.Route, route.Provider, status, requestBytes, time.Since(started).Milliseconds(),
	)
	if reason != "" {
		message += " reason=" + reason
	}
	log.Print(message)
}

func writeNotFound(writer http.ResponseWriter) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusNotFound)
	_, _ = io.WriteString(writer, `{"error":"not found"}`+"\n")
}

func copyHeaders(destination, source http.Header) {
	for key, values := range source {
		for _, value := range values {
			destination.Add(key, value)
		}
	}
}

func removeHopByHopHeaders(headers http.Header) {
	for _, token := range strings.Split(headers.Get("Connection"), ",") {
		headers.Del(strings.TrimSpace(token))
	}
	for _, header := range hopByHopHeaders {
		headers.Del(header)
	}
}

func cloneURL(source *url.URL) *url.URL {
	cloned := *source
	return &cloned
}
