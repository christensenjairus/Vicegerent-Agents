package main

import "testing"

func TestValidateListenAddress(t *testing.T) {
	// Both spellings are the same dual-stack socket, and [::] is what the process
	// logs after binding 0.0.0.0, so rejecting it would reject its own bind.
	valid := []string{
		"0.0.0.0:8081",
		"[::]:8081",
	}
	for _, address := range valid {
		if err := validateListenAddress(address); err != nil {
			t.Errorf("validateListenAddress(%q) = %v, want nil", address, err)
		}
	}

	invalid := []string{
		"",
		"127.0.0.1",
		"127.0.0.1:8081",
		"10.0.0.7:8081",
		"[::1]:8081",
		"localhost:8081",
		"webhook-listener.webhooks.svc.cluster.local:8081",
	}
	for _, address := range invalid {
		if err := validateListenAddress(address); err == nil {
			t.Errorf("validateListenAddress(%q) = nil, want error", address)
		}
	}
}
