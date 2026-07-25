package server

// Secret redaction for MCP tool-call payloads. The egress-proxy
// (charts/egress-proxy) scrubs the same credential-shaped patterns from EVERY
// outbound request/response, internal agentgateway/vMCP traffic included, but it
// works at the transport layer -- HTTP headers, URL, and body. It does not
// semantically parse the JSON-RPC tool-call ARGUMENT/RESULT payloads riding on
// the MCP connection, and it skips streaming (SSE) response bodies, which is how
// vMCP returns tool results. So if an agent reads a credential-shaped string from
// anywhere (a file, a log, a prior tool result) and passes it into a Jira comment
// body, a GitHub PR description, a Linear issue, etc., it can slip past the
// transport-layer scrub untouched.
//
// This closes that gap at the one place that sees every tool call in both
// directions regardless of destination backend: CheckRequest (before a
// call reaches vMCP) and CheckResponse (before a tool's result reaches the
// model).
//
// Deliberately mutate, never deny: a false-positive match on a
// legitimate-looking-but-harmless string (e.g. a Bearer-token-shaped test
// fixture, a base64 blob that happens to match a Slack token's charset)
// should not break an otherwise-valid call. This mirrors the egress-proxy's
// own posture (redact and forward, never 403 on a matched pattern alone)
// and the existing mutate() path already used for GitHub's forced-draft
// override -- redaction is just another argument rewrite. This is
// pattern-based and does NOT catch encoded forms (base64, hex, rot13, etc.)
// -- it raises the bar against copy-pasted plaintext secrets, not a
// determined exfiltration attempt. Cerbos-level project/team/service scoping
// is still the primary control against a MISDIRECTED call; this is a
// defense-in-depth layer against a plaintext secret riding along inside an
// otherwise-authorized one.
//
// The pattern set is the single source of truth in secret-patterns.json (this
// directory), embedded here at build time via //go:embed and rendered into the
// egress-proxy scrub.py at install time via `helm --set-file secretPatterns=...`
// -- so this Go shim and the Python egress scrubber compile the identical shapes
// from one file, with no hand-sync between the two runtimes. (The egress proxy
// additionally scrubs raw HTTP Authorization / API-key HEADERS with its own
// Python-only logic; this shim only ever sees JSON-RPC bodies, so it carries
// only the body pattern set.)

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
)

// secretPattern is one named, independently-testable entry in the redaction
// registry. name exists so a future addition/removal/tweak can be pointed at
// directly in a test case or a log line without re-deriving "which regex is
// this" from position in a slice -- see secretPatternRegistry below for the
// add-a-new-type contract.
type secretPattern struct {
	name string
	re   *regexp.Regexp
}

//go:embed secret-patterns.json
var secretPatternsJSON []byte

// secretPatternRegistry is the compiled pattern list every redaction call walks,
// loaded once at package load from the embedded secret-patterns.json (this
// directory). That JSON is the single source of truth: it is embedded here for
// the Go shim and rendered into the egress-proxy scrub.py at install time via
// `helm --set-file secretPatterns=...`, so both runtimes match the identical
// shapes from one file.
//
// To add a new well-known secret type: append ONE {"name","regex"} entry to
// secret-patterns.json (RE2-safe: no lookaround/backreferences, so the same
// string compiles in Go regexp and Python re) and add ONE corresponding case to
// TestRedactString's table (internal/server/server_test.go) with a
// fake-but-shaped fixture built via string concatenation (see that test's own
// fixtures -- literal secret-shaped constants trip this sandbox's own
// commit-time scanner and CI's detect-secrets/detect-private-key hooks). Nothing
// else needs touching: redactString/redactValue/redactRawJSON all iterate this
// slice generically.
//
// The list is NOT exhaustive and isn't meant to be -- it's deliberately biased
// toward the credential shapes most likely to leak through an agent's own tool
// calls (API keys/tokens copied from a file, a log, or a prior tool result and
// pasted into a Jira comment, GitHub PR body, Linear issue, etc.), not a
// general-purpose secret scanner. Add an entry whenever a new leak vector shows
// up in practice; don't hold out for a "complete" list first.
var secretPatternRegistry = mustCompilePatterns(secretPatternsJSON)

// mustCompilePatterns unmarshals the embedded pattern JSON and compiles every
// regex. It panics on a malformed file or an uncompilable pattern: that can only
// be a build/release-time bug in secret-patterns.json, caught immediately by the
// Go tests (and never reachable at runtime, since the input is embedded, not
// user-supplied).
func mustCompilePatterns(raw []byte) []secretPattern {
	var defs []struct {
		Name  string `json:"name"`
		Regex string `json:"regex"`
	}
	if err := json.Unmarshal(raw, &defs); err != nil {
		panic(fmt.Sprintf("secrets_redact: parsing embedded secret-patterns.json: %v", err))
	}
	out := make([]secretPattern, len(defs))
	for i, d := range defs {
		out[i] = secretPattern{name: d.Name, re: regexp.MustCompile(d.Regex)}
	}
	return out
}

const redactedPlaceholder = "<masked>"

// redactString scrubs s by walking the secretPatternRegistry, returning the
// scrubbed string and the total number of replacements. Each pattern overwrites
// its matches with redactedPlaceholder before the next pattern sees the string.
func redactString(s string) (string, int) {
	total := 0
	for _, p := range secretPatternRegistry {
		var n int
		s, n = redactPattern(p.re, s)
		total += n
	}
	return s, total
}

// redactPattern is split out from redactString so the count of matches (not
// just whether ReplaceAllString changed anything) is accurate -- regexp has
// no ReplaceAllStringFunc-with-count helper, so this counts matches via
// FindAllStringIndex first.
func redactPattern(pat *regexp.Regexp, s string) (string, int) {
	matches := pat.FindAllStringIndex(s, -1)
	if len(matches) == 0 {
		return s, 0
	}
	return pat.ReplaceAllString(s, redactedPlaceholder), len(matches)
}

// marshalNoHTMLEscape is json.Marshal without Go's default HTML escaping of
// <, >, and &. The redaction placeholder is "<masked>"; encoding/json's
// default escaper would emit it on the wire as "\u003cmasked\u003e", which
// decodes back to "<masked>" but no longer matches the literal placeholder the
// egress-proxy scrub.py and agentgateway both emit -- defeating the point of
// the shared format. MCP JSON-RPC payloads are never embedded in HTML, so the
// escaping buys nothing here. Encoder.Encode appends a trailing newline that
// Marshal does not; trim it so this is a drop-in replacement.
func marshalNoHTMLEscape(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// redactValue walks an arbitrary JSON-decoded value (the shape
// encoding/json produces from an `any`: map[string]any, []any, string,
// float64, bool, nil) and redacts every string it finds, including strings
// nested inside maps/arrays at any depth. This also catches secrets
// embedded inside a JSON-encoded STRING value (e.g. Jira's raw
// additional_fields/fields JSON arg, or a tool result's content[].text
// that itself happens to be JSON) by attempting a nested json.Unmarshal on
// any string that parses as JSON before falling back to a flat string
// redaction -- so a secret smuggled one level of JSON-string-encoding deep
// is still caught, matching the same "don't trust a single encoding layer"
// posture jiraFieldsAttr already applies for epicKey/parent smuggling.
// Returns the (possibly rewritten) value and the total redaction count.
func redactValue(v any) (any, int) {
	switch t := v.(type) {
	case string:
		return redactStringValue(t)
	case map[string]any:
		total := 0
		out := make(map[string]any, len(t))
		for k, val := range t {
			newVal, n := redactValue(val)
			out[k] = newVal
			total += n
		}
		return out, total
	case []any:
		total := 0
		out := make([]any, len(t))
		for i, val := range t {
			newVal, n := redactValue(val)
			out[i] = newVal
			total += n
		}
		return out, total
	default:
		// number, bool, nil -- nothing to redact.
		return v, 0
	}
}

// redactStringValue handles the string case for redactValue: if the string
// itself parses as JSON (an object or array), recurse into the parsed
// structure and re-encode; otherwise apply flat pattern redaction. A
// string that merely LOOKS like JSON but fails to parse (or parses to a
// scalar) falls through to flat redaction, same as any other string.
func redactStringValue(s string) (string, int) {
	trimmed := s
	if len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
		var nested any
		if err := json.Unmarshal([]byte(s), &nested); err == nil {
			switch nested.(type) {
			case map[string]any, []any:
				redacted, n := redactValue(nested)
				if n > 0 {
					if reEncoded, err := marshalNoHTMLEscape(redacted); err == nil {
						return string(reEncoded), n
					}
				}
				return s, 0
			}
		}
	}
	return redactString(s)
}

// redactArguments redacts every string value in a tool-call's arguments map,
// returning a new map (the input is not mutated in place) and the total
// redaction count.
func redactArguments(args map[string]any) (map[string]any, int) {
	redacted, n := redactValue(args)
	out, _ := redacted.(map[string]any)
	if out == nil {
		out = map[string]any{}
	}
	return out, n
}

// redactRawJSON redacts every string value found by decoding raw as JSON,
// re-encoding the result. Used for tool RESULT bytes (McpResponse's
// mcp_response field), whose top-level shape is the JSON-RPC "result"
// object, not a plain arguments map. Returns the original bytes unchanged
// (with n=0) if raw isn't valid JSON or nothing matched -- callers should
// pass raw through unmutated in that case rather than risk a matched-but-
// unparseable response subtly changing behavior.
func redactRawJSON(raw []byte) ([]byte, int) {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return raw, 0
	}
	redacted, n := redactValue(decoded)
	if n == 0 {
		return raw, 0
	}
	reEncoded, err := marshalNoHTMLEscape(redacted)
	if err != nil {
		return raw, 0
	}
	return reEncoded, n
}
