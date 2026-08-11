package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strings"
)

var routeNamePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
var secretFilePattern = regexp.MustCompile(`^[a-z][a-z0-9_.-]{0,252}$`)

// validRouteName rejects consecutive hyphens in addition to the pattern: the
// installer derives a per-route env var by uppercasing the name and mapping
// "-" to "_", so "a--b" would collide with the "__" agent/route separator.
func validRouteName(name string) bool {
	return routeNamePattern.MatchString(name) && !strings.Contains(name, "--")
}

type Config struct {
	Routes []Route `json:"routes"`
}

type Route struct {
	Agent      string `json:"agent"`
	Route      string `json:"route"`
	Provider   string `json:"provider"`
	SecretFile string `json:"secretFile"`
	TargetURL  string `json:"targetURL"`
}

type compiledRoute struct {
	Route
	target *url.URL
}

func loadConfig(path string) (Config, error) {
	contents, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read routing config: %w", err)
	}

	var config Config
	if err := json.Unmarshal(contents, &config); err != nil {
		return Config{}, fmt.Errorf("parse routing config: %w", err)
	}
	return config, nil
}

func compileRoutes(config Config) (map[string]compiledRoute, error) {
	if len(config.Routes) == 0 {
		return nil, fmt.Errorf("routing config has no routes")
	}

	compiled := make(map[string]compiledRoute, len(config.Routes))
	for index, route := range config.Routes {
		if !validRouteName(route.Agent) {
			return nil, fmt.Errorf("route %d has invalid agent %q", index, route.Agent)
		}
		if !validRouteName(route.Route) {
			return nil, fmt.Errorf("route %d has invalid route name %q", index, route.Route)
		}
		if !secretFilePattern.MatchString(route.SecretFile) {
			return nil, fmt.Errorf("route %d has invalid secret file %q", index, route.SecretFile)
		}
		if !supportedProvider(route.Provider) {
			return nil, fmt.Errorf("route %d has unsupported provider %q", index, route.Provider)
		}

		target, err := url.Parse(route.TargetURL)
		if err != nil {
			return nil, fmt.Errorf("route %d has invalid target URL: %w", index, err)
		}
		expectedHost := route.Agent + "-webhook.agent-sandbox.svc.cluster.local:8644"
		expectedPath := "/webhooks/" + route.Route
		if target.Scheme != "http" || target.Host != expectedHost || target.Path != expectedPath || target.RawQuery != "" || target.Fragment != "" || target.User != nil {
			return nil, fmt.Errorf("route %d target must be exactly http://%s%s", index, expectedHost, expectedPath)
		}

		path := "/webhooks/" + route.Agent + "/" + route.Route
		if _, exists := compiled[path]; exists {
			return nil, fmt.Errorf("duplicate route path %q", path)
		}
		compiled[path] = compiledRoute{Route: route, target: target}
	}

	return compiled, nil
}
