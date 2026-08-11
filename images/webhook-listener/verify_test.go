package main

import (
	"net/http"
	"testing"
	"time"
)

var (
	fixtureBody   = []byte(`{"event":{"id":"evt-123","event_type":"incident.triggered"}}`)
	fixtureSecret = []byte("fixture-secret")
	fixtureNow    = time.Unix(1_700_000_000, 0)
)

func TestProviderSignatures(t *testing.T) {
	tests := []struct {
		name     string
		provider string
		headers  http.Header
	}{
		{
			name:     "pagerduty rotation",
			provider: "pagerduty",
			headers: http.Header{
				"X-Pagerduty-Signature": []string{"v1=0000, v1=6220f3fc1b90181b62bfd612f290bdf368b6b66b47ab91130794ad1a947e46af"},
			},
		},
		{
			name:     "github",
			provider: "github",
			headers: http.Header{
				"X-Hub-Signature-256": []string{"sha256=6220f3fc1b90181b62bfd612f290bdf368b6b66b47ab91130794ad1a947e46af"},
			},
		},
		{
			name:     "gitlab",
			provider: "gitlab",
			headers:  http.Header{"X-Gitlab-Token": []string{"fixture-secret"}},
		},
		{
			name:     "svix rotation",
			provider: "svix",
			headers: http.Header{
				"Svix-Id":        []string{"msg_test"},
				"Svix-Timestamp": []string{"1700000000"},
				"Svix-Signature": []string{"v1,AAAA v1,IPIJiBz/nw3CilyRBMdU/VuU1DF+7hEez+Y/ORTeX1Y="},
			},
		},
		{
			name:     "generic v2",
			provider: "generic-v2",
			headers: http.Header{
				"X-Webhook-Signature-V2": []string{"80f330b0a14c5b8a15081f3bcef458fd5785481d7d0e0b62170deecea6559a54"}, // pragma: allowlist secret
				"X-Webhook-Timestamp":    []string{"1700000000"},
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := verifySignature(test.provider, test.headers, fixtureBody, fixtureSecret, fixtureNow); err != nil {
				t.Fatalf("known-good signature rejected: %v", err)
			}
			if err := verifySignature(test.provider, test.headers, fixtureBody, []byte("wrong-secret"), fixtureNow); err == nil {
				t.Fatal("wrong secret accepted")
			}

			// GitLab's native token header authenticates the delivery but does not
			// bind the body. TLS supplies transport integrity for that provider.
			if test.provider == "gitlab" {
				if err := verifySignature(test.provider, test.headers, []byte("tampered"), fixtureSecret, fixtureNow); err != nil {
					t.Fatalf("GitLab token unexpectedly depends on body: %v", err)
				}
			} else if err := verifySignature(test.provider, test.headers, []byte("tampered"), fixtureSecret, fixtureNow); err == nil {
				t.Fatal("tampered body accepted")
			}
		})
	}
}

func TestAlertmanagerBearerToken(t *testing.T) {
	valid := http.Header{"Authorization": []string{"Bearer " + string(fixtureSecret)}}
	if err := verifySignature("alertmanager", valid, fixtureBody, fixtureSecret, fixtureNow); err != nil {
		t.Fatalf("valid credential rejected: %v", err)
	}

	// Alertmanager signs no payload, so the credential must not depend on the
	// body -- only on the shared token.
	if err := verifySignature("alertmanager", valid, []byte("different body"), fixtureSecret, fixtureNow); err != nil {
		t.Fatalf("alertmanager unexpectedly depends on body: %v", err)
	}

	for name, headers := range map[string]http.Header{
		"wrong token":      {"Authorization": []string{"Bearer wrong-secret"}},
		"missing scheme":   {"Authorization": []string{string(fixtureSecret)}},
		"wrong scheme":     {"Authorization": []string{"Basic " + string(fixtureSecret)}},
		"absent header":    {},
		"empty credential": {"Authorization": []string{"Bearer "}},
	} {
		if err := verifySignature("alertmanager", headers, fixtureBody, fixtureSecret, fixtureNow); err == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

func TestSvixBase64Secret(t *testing.T) {
	headers := http.Header{
		"Svix-Id":        []string{"msg_test"},
		"Svix-Timestamp": []string{"1700000000"},
		"Svix-Signature": []string{"v1,IPIJiBz/nw3CilyRBMdU/VuU1DF+7hEez+Y/ORTeX1Y="},
	}
	if err := verifySignature("svix", headers, fixtureBody, []byte("whsec_Zml4dHVyZS1zZWNyZXQ="), fixtureNow); err != nil {
		t.Fatalf("whsec_ signature rejected: %v", err)
	}
}

func TestTimestampedSignaturesRejectReplay(t *testing.T) {
	for _, test := range []struct {
		provider string
		headers  http.Header
	}{
		{
			provider: "svix",
			headers: http.Header{
				"Svix-Id":        []string{"msg_test"},
				"Svix-Timestamp": []string{"1700000000"},
				"Svix-Signature": []string{"v1,IPIJiBz/nw3CilyRBMdU/VuU1DF+7hEez+Y/ORTeX1Y="},
			},
		},
		{
			provider: "generic-v2",
			headers: http.Header{
				"X-Webhook-Signature-V2": []string{"80f330b0a14c5b8a15081f3bcef458fd5785481d7d0e0b62170deecea6559a54"}, // pragma: allowlist secret
				"X-Webhook-Timestamp":    []string{"1700000000"},
			},
		},
	} {
		if err := verifySignature(test.provider, test.headers, fixtureBody, fixtureSecret, fixtureNow.Add(6*time.Minute)); err == nil {
			t.Fatalf("%s accepted an expired signature", test.provider)
		}
	}
}
