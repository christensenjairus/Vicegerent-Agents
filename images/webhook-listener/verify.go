package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const signatureTolerance = 5 * time.Minute

var errInvalidSignature = errors.New("invalid signature")

func supportedProvider(provider string) bool {
	switch provider {
	case "pagerduty", "github", "gitlab", "svix", "generic-v2", "alertmanager":
		return true
	default:
		return false
	}
}

func verifySignature(provider string, headers http.Header, body, secret []byte, now time.Time) error {
	if len(secret) == 0 {
		return errors.New("empty signing secret")
	}

	switch provider {
	case "pagerduty":
		return verifyPagerDuty(headers.Get("X-PagerDuty-Signature"), body, secret)
	case "github":
		return verifyGitHub(headers.Get("X-Hub-Signature-256"), body, secret)
	case "gitlab":
		return verifyGitLab(headers.Get("X-Gitlab-Token"), secret)
	case "svix":
		return verifySvix(headers, body, secret, now)
	case "generic-v2":
		return verifyGenericV2(headers, body, secret, now)
	case "alertmanager":
		return verifyAlertmanager(headers, secret)
	default:
		return fmt.Errorf("unsupported signature provider %q", provider)
	}
}

func verifyPagerDuty(header string, body, secret []byte) error {
	expected := hmacSHA256(secret, body)
	for _, candidate := range strings.Split(header, ",") {
		version, encoded, ok := strings.Cut(strings.TrimSpace(candidate), "=")
		if !ok || version != "v1" {
			continue
		}
		actual, err := hex.DecodeString(encoded)
		if err == nil && hmac.Equal(actual, expected) {
			return nil
		}
	}
	return errInvalidSignature
}

func verifyGitHub(header string, body, secret []byte) error {
	prefix, encoded, ok := strings.Cut(header, "=")
	if !ok || prefix != "sha256" {
		return errInvalidSignature
	}
	actual, err := hex.DecodeString(encoded)
	if err != nil || !hmac.Equal(actual, hmacSHA256(secret, body)) {
		return errInvalidSignature
	}
	return nil
}

func verifyGitLab(header string, secret []byte) error {
	if !hmac.Equal([]byte(header), secret) {
		return errInvalidSignature
	}
	return nil
}

func verifySvix(headers http.Header, body, secret []byte, now time.Time) error {
	messageID := headers.Get("Svix-Id")
	timestamp := headers.Get("Svix-Timestamp")
	signatures := headers.Get("Svix-Signature")
	if messageID == "" || timestamp == "" || signatures == "" {
		return errInvalidSignature
	}
	if err := validateTimestamp(timestamp, now); err != nil {
		return err
	}

	key := secret
	if strings.HasPrefix(string(secret), "whsec_") {
		decoded, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(string(secret), "whsec_"))
		if err != nil {
			return errInvalidSignature
		}
		key = decoded
	}
	signed := append([]byte(messageID+"."+timestamp+"."), body...)
	expected := hmacSHA256(key, signed)
	for _, candidate := range strings.Fields(signatures) {
		version, encoded, ok := strings.Cut(candidate, ",")
		if !ok || version != "v1" {
			continue
		}
		actual, err := base64.StdEncoding.DecodeString(encoded)
		if err == nil && hmac.Equal(actual, expected) {
			return nil
		}
	}
	return errInvalidSignature
}

// verifyAlertmanager authenticates Prometheus Alertmanager deliveries.
// Alertmanager signs no payload, so a route-scoped static credential sent in
// the Authorization header is the authentication mechanism. Compared in
// constant time; the header is stripped before the request reaches the agent.
func verifyAlertmanager(headers http.Header, secret []byte) error {
	token, ok := strings.CutPrefix(headers.Get("Authorization"), "Bearer ")
	if !ok {
		return errInvalidSignature
	}
	if !hmac.Equal([]byte(strings.TrimSpace(token)), secret) {
		return errInvalidSignature
	}
	return nil
}

func verifyGenericV2(headers http.Header, body, secret []byte, now time.Time) error {
	signature := headers.Get("X-Webhook-Signature-V2")
	timestamp := headers.Get("X-Webhook-Timestamp")
	if signature == "" || timestamp == "" {
		return errInvalidSignature
	}
	if err := validateTimestamp(timestamp, now); err != nil {
		return err
	}
	actual, err := hex.DecodeString(signature)
	if err != nil {
		return errInvalidSignature
	}
	signed := append([]byte(timestamp+"."), body...)
	if !hmac.Equal(actual, hmacSHA256(secret, signed)) {
		return errInvalidSignature
	}
	return nil
}

func validateTimestamp(raw string, now time.Time) error {
	seconds, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return errInvalidSignature
	}
	delta := now.Sub(time.Unix(seconds, 0))
	if delta < -signatureTolerance || delta > signatureTolerance {
		return errors.New("signature timestamp outside replay window")
	}
	return nil
}

func hmacSHA256(secret, body []byte) []byte {
	digest := hmac.New(sha256.New, secret)
	_, _ = digest.Write(body)
	return digest.Sum(nil)
}
