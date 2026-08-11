// Package promptinjection provides two-stage detection of prompt-injection
// shapes in free text returned by external tool-read results (a scraped
// webpage via Firecrawl/Tavily, a fetched Notion/Jira/Confluence page body, a
// GitHub file, a GitLab merge-request diff, ...). Untrusted content flowing
// INTO the agent's context from a tool RESULT is a different risk than
// internal/moderation's outbound content-moderation gate, which checks
// free-text arguments of WRITE calls flowing OUT to Notion/Linear/GitHub/etc.
// before they reach Cerbos -- this package has no relationship to that one
// beyond sharing the "registry of regexes run over free text" shape (see
// secrets_redact.go's secretPatternRegistry, this package's closest
// structural sibling in this codebase) for its first stage.
//
// Stage 1 (this file's InjectionPatternRegistry + RegexDetector) is
// deliberately broad/high-recall: it is EXPECTED to over-match legitimate
// content (a security blog post discussing "ignore previous instructions"
// attacks, documentation that quotes a jailbreak prompt as an example,
// ...). That's fine by design -- it's cheap, runs on every eligible
// response, and exists purely to cut the volume of text that needs the
// expensive stage 2 (judge.go) LLM-judge call down to the rare subset that
// actually matched something. Stage 2 is what filters that recall down to
// a confirmed detection worth blocking on -- see judge.go's doc comment.
// Blocking behavior itself (as opposed to log-only) lives in server.go's
// checkPromptInjection/CheckResponse wiring, not in this package.
package promptinjection

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
)

// injectionPattern is one named, independently-testable entry in the
// stage-1 detection registry -- mirrors secretPatternRegistry's shape
// (internal/server/secrets_redact.go) so both registries stay easy to
// extend the same way: append one entry, no other code changes needed.
type injectionPattern struct {
	name string
	re   *regexp.Regexp
}

// InjectionPatternRegistry is compiled from the embedded canonical patterns.json
// list that every stage-1 Detect call walks.
// Exported (unlike secretPatternRegistry) so a future admin tool or test
// fixture outside this package can enumerate known pattern names without
// reaching into an unexported var.
//
// Deliberately over-broad (recall over precision): every entry here is
// written to catch a known injection SHAPE as cheaply as possible, accepting
// that it will also fire on benign text that merely discusses, quotes, or
// documents that shape (a security writeup, a support article, this very
// source file's own comments). That tradeoff is safe specifically because a
// stage-1 match alone never blocks anything -- it only gates whether the
// (comparatively expensive) stage-2 LLM judge runs at all. This list is NOT
// exhaustive and isn't meant to be; add a canonical JSON entry whenever a new
// shape shows up in practice (real traffic, red-team exercises, or a public
// writeup), same "don't hold out for complete" posture as secretPatternRegistry.
//
//go:embed patterns.json
var patternDefinitionsJSON []byte

type patternDefinition struct {
	Name  string `json:"name"`
	Regex string `json:"regex"`
}

func mustLoadPatternRegistry() []injectionPattern {
	var definitions []patternDefinition
	if err := json.Unmarshal(patternDefinitionsJSON, &definitions); err != nil {
		panic(fmt.Sprintf("parse embedded prompt-injection patterns: %v", err))
	}
	if len(definitions) == 0 {
		panic("embedded prompt-injection pattern registry is empty")
	}
	registry := make([]injectionPattern, 0, len(definitions))
	for _, definition := range definitions {
		if definition.Name == "" || definition.Regex == "" {
			panic("embedded prompt-injection pattern has an empty name or regex")
		}
		registry = append(registry, injectionPattern{
			name: definition.Name,
			re:   regexp.MustCompile(definition.Regex),
		})
	}
	return registry
}

var InjectionPatternRegistry = mustLoadPatternRegistry()

// Detector reports whether s contains a known injection shape. Production
// uses *RegexDetector; tests substitute a stub. This is the stage-1
// interface only -- stage 2 (LLM-judge confirmation) is Judge, below.
type Detector interface {
	Detect(s string) *Result
}

// maxOffsetsPerPattern bounds how many occurrences of a single pattern
// Detect records. Without a cap, a pathological response (many repeats of
// a benign-looking trigger phrase) could otherwise generate an unbounded
// number of stage-2 judge calls -- see checkPromptInjection's own
// maxJudgeCallsPerResponse budget in server.go for the response-wide cap
// that backstops this per-pattern one.
const maxOffsetsPerPattern = 8

// Result is the outcome of a stage-1 Detect call.
type Result struct {
	Matched bool
	// MatchedNames holds one entry per (pattern, occurrence) pair -- a
	// pattern that matches N times (N capped at maxOffsetsPerPattern)
	// appears N times, once per occurrence, so callers can judge each
	// occurrence independently instead of only the first (a real
	// injection placed after an earlier benign match of the same pattern
	// -- e.g. "ignore previous instructions" first appearing inside a
	// sentence *describing* the attack, with the actual attack later in
	// the same document -- must not be able to hide behind that first,
	// judged-benign occurrence).
	MatchedNames []string
	// MatchedOffsets holds the byte offset of each occurrence, same
	// order/length as MatchedNames -- callers use this to build the
	// bounded text window Judge.Confirm scans (see judge.go), so the
	// judge sees only the text around each match rather than the whole
	// document.
	MatchedOffsets []int
}

// RegexDetector walks InjectionPatternRegistry over a string.
type RegexDetector struct{}

// New constructs a RegexDetector.
func New() *RegexDetector {
	return &RegexDetector{}
}

// Detect walks InjectionPatternRegistry against s and returns every
// occurrence of every pattern that matched, up to maxOffsetsPerPattern
// occurrences per pattern (empty/non-matching input returns a non-matched
// Result, never nil). Reports ALL occurrences, not just the first -- a
// single FindStringIndex-per-pattern call would let a real injection later
// in the same string hide behind an earlier, judged-benign match of the
// same pattern name.
func (d *RegexDetector) Detect(s string) *Result {
	var names []string
	var offsets []int
	for _, p := range InjectionPatternRegistry {
		locs := p.re.FindAllStringIndex(s, maxOffsetsPerPattern)
		for _, loc := range locs {
			names = append(names, p.name)
			offsets = append(offsets, loc[0])
		}
	}
	return &Result{Matched: len(names) > 0, MatchedNames: names, MatchedOffsets: offsets}
}
