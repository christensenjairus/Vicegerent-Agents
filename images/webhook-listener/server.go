package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxBodyBytes = 1 << 20

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
		now: time.Now,
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

	status, headerWritten, err := server.forward(writer, request, body, route)
	if err != nil {
		if headerWritten {
			// The upstream status and headers were already sent, so we can only
			// abandon the truncated response -- not replace it with a 502.
			logDelivery(route, "forward_truncated", status, len(body), started, "response_copy_failed")
			return
		}
		http.Error(writer, "upstream unavailable", http.StatusBadGateway)
		logDelivery(route, "forward_failed", http.StatusBadGateway, len(body), started, "upstream_unavailable")
		return
	}
	logDelivery(route, "forwarded", status, len(body), started, "")
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
	stripSignatureHeaders(outbound.Header)

	response, err := server.client.Do(outbound)
	if err != nil {
		return 0, false, err
	}
	defer response.Body.Close()

	copyHeaders(writer.Header(), response.Header)
	removeHopByHopHeaders(writer.Header())
	writer.WriteHeader(response.StatusCode)
	_, err = io.Copy(writer, response.Body)
	return response.StatusCode, true, err
}

// isSignatureHeader reports whether a header carries webhook authentication
// material -- a signature, a shared token, or an Authorization credential.
// Matching by shape rather than an exact denylist strips every provider's
// signing input, current or future (e.g. GitHub's legacy X-Hub-Signature and
// Svix's unbranded Webhook-Signature), before the delivery reaches the agent.
func isSignatureHeader(name string) bool {
	lower := strings.ToLower(name)
	if lower == "authorization" {
		return true
	}
	return strings.Contains(lower, "signature") || strings.Contains(lower, "token")
}

func stripSignatureHeaders(headers http.Header) {
	for name := range headers {
		if isSignatureHeader(name) {
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
