package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	ngrok "golang.ngrok.com/ngrok/v2"
)

const (
	defaultConfigPath = "/etc/vicegerent-webhooks/routes.json"
	defaultSecretRoot = "/var/run/secrets/vicegerent-webhooks"
	healthAddress     = "127.0.0.1:8080"
	// version is reported to ngrok as the agent client version; keep it in step
	// with the image tag in Makefile and the chart deployment.
	version = "v0.1.6"
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
	publicURL := os.Getenv("WEBHOOK_PUBLIC_URL")
	authtoken := os.Getenv("NGROK_AUTHTOKEN")
	if publicURL == "" {
		return fmt.Errorf("WEBHOOK_PUBLIC_URL is required")
	}
	if authtoken == "" {
		return fmt.Errorf("NGROK_AUTHTOKEN is required")
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

	agent, err := ngrok.NewAgent(
		ngrok.WithAuthtoken(authtoken),
		ngrok.WithClientInfo("vicegerent-webhook-listener", version),
	)
	if err != nil {
		return fmt.Errorf("create ngrok agent: %w", err)
	}
	listener, err := agent.Listen(ctx, ngrok.WithURL(publicURL))
	if err != nil {
		return fmt.Errorf("open ngrok endpoint: %w", err)
	}
	defer listener.Close()
	close(ready)
	log.Printf("ngrok endpoint ready url=%s routes=%d", listener.URL().String(), len(config.Routes))

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
	_ = agent.Disconnect()
	if serveErr != nil && serveErr != http.ErrServerClosed {
		return serveErr
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
