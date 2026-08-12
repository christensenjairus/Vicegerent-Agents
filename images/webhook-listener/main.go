package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const (
	defaultConfigPath    = "/etc/vicegerent-webhooks/routes.json"
	defaultSecretRoot    = "/var/run/secrets/vicegerent-webhooks"
	defaultListenAddress = "0.0.0.0:8081"
	healthAddress        = "127.0.0.1:8080"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		if err := checkHealth(); err != nil {
			log.Fatal(err)
		}
		return
	}
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	configPath := envOrDefault("WEBHOOK_CONFIG", defaultConfigPath)
	secretRoot := envOrDefault("WEBHOOK_SECRET_ROOT", defaultSecretRoot)
	listenAddress := envOrDefault("WEBHOOK_LISTEN_ADDRESS", defaultListenAddress)
	if err := validateListenAddress(listenAddress); err != nil {
		return err
	}

	config, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	transport, err := newForwardTransport(os.Getenv("WEBHOOK_FORWARD_PROXY"))
	if err != nil {
		return err
	}
	handler, err := newWebhookServer(config, secretRoot, transport)
	if err != nil {
		return err
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	ready := make(chan struct{})
	healthServer := &http.Server{
		Addr:              healthAddress,
		ReadHeaderTimeout: 2 * time.Second,
		Handler: http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.URL.Path != "/healthz" {
				http.NotFound(writer, request)
				return
			}
			select {
			case <-ready:
				writer.WriteHeader(http.StatusOK)
				_, _ = io.WriteString(writer, "ok\n")
			default:
				http.Error(writer, "not ready", http.StatusServiceUnavailable)
			}
		}),
	}
	healthErrors := make(chan error, 1)
	go func() {
		healthErrors <- healthServer.ListenAndServe()
	}()

	listener, err := net.Listen("tcp", listenAddress)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", listenAddress, err)
	}
	defer listener.Close()
	close(ready)
	log.Printf("webhook listener ready address=%s routes=%d", listener.Addr().String(), len(config.Routes))

	publicServer := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	publicErrors := make(chan error, 1)
	go func() {
		publicErrors <- publicServer.Serve(listener)
	}()

	var serveErr error
	select {
	case <-ctx.Done():
	case serveErr = <-publicErrors:
	case serveErr = <-healthErrors:
	}

	shutdownContext, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	_ = publicServer.Shutdown(shutdownContext)
	_ = healthServer.Shutdown(shutdownContext)
	if serveErr != nil && serveErr != http.ErrServerClosed {
		return serveErr
	}
	return nil
}

// validateListenAddress keeps the public handler on a wildcard address, which is
// the pod-wide address the listener Service targets, so a loopback or single-IP
// bind cannot silently make the pod unreachable. 0.0.0.0 and :: name the same
// dual-stack socket here -- Go reports either bind as [::] -- so both spellings
// are accepted. Restricting who may connect is the listener's Cilium policy,
// which admits only the cloudflared workload.
func validateListenAddress(address string) error {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return fmt.Errorf("parse WEBHOOK_LISTEN_ADDRESS: %w", err)
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsUnspecified() {
		return fmt.Errorf("WEBHOOK_LISTEN_ADDRESS must bind a wildcard address (0.0.0.0 or ::)")
	}
	return nil
}

func newForwardTransport(rawURL string) (*http.Transport, error) {
	proxyURL, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("parse WEBHOOK_FORWARD_PROXY: %w", err)
	}
	if proxyURL.Scheme != "http" || proxyURL.Host == "" || proxyURL.User != nil || proxyURL.Path != "" || proxyURL.RawQuery != "" || proxyURL.Fragment != "" {
		return nil, fmt.Errorf("WEBHOOK_FORWARD_PROXY must be an HTTP origin")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxyURL)
	return transport, nil
}

func checkHealth() error {
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://" + healthAddress + "/healthz")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("health endpoint returned %s", response.Status)
	}
	return nil
}

func envOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
