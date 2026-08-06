// Package server implements the agentgateway ExtMcp gRPC service.
// Fail-closed contract: only tools/call is evaluated for Cerbos authz; bad
// params/mapping/eval/Cerbos errors deny. Responses are pass or error, except
// a tool with a mapping `force` set, which allows via a mutated
// (rewritten-args) result instead of a bare pass — never on a denied call.
package server

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"
	"time"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/authz"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/moderation"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/promptinjection"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

// internalBackendName is the distinct MCP target name used by the shim's
// re-entrant route. Its required methods are request-only. ExtMcp service_names
// carries this target name, not the AgentgatewayBackend resource name.
const internalBackendName = "vmcp-internal"

const toolsCall = "tools/call"

var internalAllowedRequestMethods = map[string]bool{
	"initialize":                true,
	"notifications/initialized": true,
	"tools/list":                true,
	toolsCall:                   true,
}

const resourcesRead = "resources/read"
const promptsGet = "prompts/get"
const tasksGet = "tasks/get"
const tasksUpdate = "tasks/update"

var redactableResponseMethods = map[string]bool{
	toolsCall:     true,
	resourcesRead: true,
	promptsGet:    true,
	tasksGet:      true,
	tasksUpdate:   true,
}

// The Notion existing-page-write ancestry gate keys off the mapped resource,
// not the tool-name string, so renaming a tool in mapping.yaml keeps the gate
// intact as long as it keeps one of these resourceType/action pairs (matches
// the rules in charts/cerbos-policies/policies/resource_notion.yaml).
// notion-create-pages is NOT one of these -- it's still pinned to
// Scratchpad-only via its own Cerbos deny rule
// (deny-create-outside-scratchpad), a narrower and separate policy from this
// multi-parent allowlist for calls that target an EXISTING page.
const (
	notionPageResource  = "notion_page"
	notionUpdateAction  = "update"
	notionCommentAction = "comment"
)

// notionAncestryGatedActions is the set of notion_page actions the ancestry
// gate applies to -- every write against an EXISTING page by id. create-pages
// deliberately isn't here (see comment above).
var notionAncestryGatedActions = map[string]bool{
	notionUpdateAction:  true,
	notionCommentAction: true,
}

// linearSaveCommentTool, linearSaveIssueTool, linearSaveProjectTool, and
// linearTeamResource identify the Linear write calls the team-resolution
// gates apply to, all mapped to linear_team/access (mapping.yaml). Keying
// off the tool name (not just the resource/action pair, unlike the Notion
// gate above) because all three tools share that exact same
// resourceType/action, and each needs different resolution logic:
//   - linear_save_comment: never carries a team of its own --
//     always resolved via issueId lookup when issueId is set.
//   - linear_save_issue: an EXPLICIT `team` arg (create, or a
//     deliberate update reassignment) is already a directly-verifiable
//     signal populated by linearIssueAttr and must NOT be re-resolved or
//     overridden. Only an UPDATE call that omits `team` entirely gets a
//     lookup here, resolving the issue's CURRENT team by its `id` -- this
//     closes the gap where an ordinary field edit on an out-of-allowlist-team
//     issue previously fell through to allow-all with no teamId attr at all.
//   - linear_save_project: same shape as save_issue -- an explicit
//     addTeams/setTeams arg is already verifiable via linearProjectAttr and
//     is never re-resolved; only an update that sets NEITHER gets a lookup
//     here, resolving the project's CURRENT teams by its `id`.
const (
	linearSaveCommentTool = "linear_save_comment"
	linearSaveIssueTool   = "linear_save_issue"
	linearSaveProjectTool = "linear_save_project"
	linearTeamResource    = "linear_team"
)

// pagerdutyManageIncidentsTools/pagerdutyAddNoteTools/pagerdutyIncidentResource
// identify the PagerDuty write calls the service-resolution gate applies to,
// one entry per backend this shim fronts (toolhive-servers.json: pagerduty,
// pagerduty_secondary). Unlike the Linear gates above, neither tool's own args
// carry ANYTHING that identifies the incident's owning service directly --
// only an opaque incident_id/incident_ids. The gate resolves each targeted
// incident to its service via a live get_incident lookup and hands the
// resolved id(s) to Cerbos's existing service allowlist rule
// (resource_pagerduty.yaml), the same handoff pattern as the Linear
// issue/project team gates.
//
// Each map's value is that SAME backend's own get_incident tool name -- the
// live lookup must query the backend the incident actually lives in, not
// always the first-registered one, or every gov call fails closed with a
// "not found" from looking the incident up in the wrong PagerDuty account
// (get_incident itself stays unmapped in Cerbos for both backends, for the
// same recursion-safety reason notion_notion-fetch/linear_get_issue are
// documented elsewhere in this shim: a deny rule on it would make every
// manage_incidents/add_note_to_incident lookup fail closed unconditionally
// instead of the intended per-call, service-scoping-tied check).
var (
	pagerdutyManageIncidentsTools = map[string]string{
		"pagerduty_manage_incidents":           "pagerduty_get_incident",
		"pagerduty_secondary_manage_incidents": "pagerduty_secondary_get_incident",
	}
	pagerdutyAddNoteTools = map[string]string{
		"pagerduty_add_note_to_incident":           "pagerduty_get_incident",
		"pagerduty_secondary_add_note_to_incident": "pagerduty_secondary_get_incident",
	}
)

const pagerdutyIncidentResource = "pagerduty_incident"

// githubRepoResource and githubExistingPRTools identify the GitHub write
// calls the PR-author resolution gate applies to: every tool that targets
// an EXISTING pull request by pullNumber. create_pull_request has no prior
// PR to check (nothing to look up, and the bot's own token authors it), and
// the read tools (pull_request_read, list/search_pull_requests, get_me)
// carry no ownership risk. Unlike the Linear/PagerDuty gates above, there is
// no argument on any of these tools that could ever supply "author" directly
// -- a PR's author isn't reassignable via any of these tools -- so this gate
// always resolves live state, never a caller-supplied signal.
const githubRepoResource = "github_repo"

var githubExistingPRTools = map[string]bool{
	"github_update_pull_request":        true,
	"github_update_pull_request_branch": true,
	"github_request_copilot_review":     true,
}

// gitlabProjectResource and gitlabExistingMRTools identify the GitLab write
// calls the MR-author resolution gate applies to -- the GitLab counterpart of
// githubExistingPRTools above, same "a hallucinated resource id must not be
// writable" rationale. Only update_merge_request qualifies: it is the one
// mapped tool that mutates an EXISTING merge request's own fields (title/
// description/labels/target_branch/state_event). create_merge_request has no
// prior MR to check, the reads carry no ownership risk, and the MR note/thread/
// discussion/draft-note WRITE tools are not in the allowlist at all -- the
// operator does not want the bot leaving any comment text on an MR under their
// identity, the same call made on the GitHub side, so there is nothing for this
// gate to cover there (only the READ note/discussion tools remain). As with
// GitHub, no argument on this tool could ever supply "author" directly -- an
// MR's author isn't reassignable -- so this gate always resolves live state.
//
// NOT covered, and deliberately so: gitlab_update_issue and gitlab_create_issue
// mutate ISSUES, which have no author-ownership gate on either forge. GitLab's
// tool surface simply has issue tools where GitHub's allowlist has none, so
// there is no GitHub rule to mirror; both are still project-scoped by
// deny-non-allowed-project. See resource_gitlab.yaml's header.
const gitlabProjectResource = "gitlab_project"

var gitlabExistingMRTools = map[string]bool{
	"gitlab_update_merge_request": true,
}

// jiraProjectResource and jiraAssigneeGatedTools identify the Jira write
// calls the ticket-assignee resolution gate applies to: update_issue,
// add_comment, and transition_issue -- tools that directly edit/comment/
// transition an EXISTING issue's own content. create_issue_link/
// link_to_epic relate two already-project-scoped tickets rather than edit
// either ticket's own content, and create_issue is already covered by the
// existing arg-based deny-create-missing-assignee rule (jiraFieldsAttr
// surfaces its top-level assignee arg directly) -- neither needs this gate.
const jiraProjectResource = "jira_project"

var jiraAssigneeGatedTools = map[string]bool{
	"jira_jira_update_issue":     true,
	"jira_jira_add_comment":      true,
	"jira_jira_transition_issue": true,
}

// alertmanagerSilenceResource and alertmanagerDeleteSilenceTools identify the
// Alertmanager write calls the silence-owner resolution gate applies to:
// deleteSilence, one entry per backend this shim fronts (toolhive-servers.json:
// alertmanager, alertmanager_secondary). Unlike the Linear/PagerDuty gates above,
// deleteSilence's own args carry only an opaque silenceId -- nothing that
// identifies the silence's real creator directly. The gate resolves the
// target silence's REAL createdBy via a live getSilences lookup and hands it
// to Cerbos's deny-not-own-silence rule (resource_alertmanager.yaml), the
// same handoff pattern as the other live-resolved gates in this file.
// createSilence's OWN createdBy is never read here -- it's unconditionally
// forced to ${alertmanagerCreatedBy} via mapping.yaml's `force` mechanism, so
// every silence this shim creates already carries the value this gate later
// checks for.
//
// Each map's value is that SAME backend's own getSilences tool name -- the
// live lookup must query the backend the silence actually lives in, same
// rationale as pagerdutyManageIncidentsTools above. getSilences itself stays
// unmapped in Cerbos for both backends, same recursion-safety reason
// pagerduty_*_get_incident/notion_notion-fetch/linear_get_issue are unmapped.
var alertmanagerDeleteSilenceTools = map[string]string{
	"alertmanager_deleteSilence":           "alertmanager_getSilences",
	"alertmanager_secondary_deleteSilence": "alertmanager_secondary_getSilences",
}

const alertmanagerSilenceResource = "alertmanager_silence"

// DefaultModeratedWriteVerbs are tool-name substrings identifying a WRITE
// call likely to carry free text a human will read. A verb heuristic
// instead of a hand-enumerated tool list, so new write tools are covered
// automatically. Deliberately excludes bare "comment" (false-matches
// read tools like notion-get-comments) in favor of "add_comment"/"create_comment".
var DefaultModeratedWriteVerbs = []string{
	"create",
	"update",
	"save",
	"add_note",
	"add_comment",
	"transition",
}

// isModeratedWriteTool reports whether toolName matches any verb in verbs,
// case-insensitively, as a substring.
func isModeratedWriteTool(toolName string, verbs []string) bool {
	lower := strings.ToLower(toolName)
	for _, verb := range verbs {
		if strings.Contains(lower, verb) {
			return true
		}
	}
	return false
}

// upstreamLookupTimeout bounds a single live shim->vMCP lookup call (Notion
// ancestry, Linear issue-team resolution) so one gated tools/call can't hang
// the whole CheckRequest (the gateway is FailClosed, so a hang would deny
// anyway — but only after its own longer timeout, holding the connection
// open meanwhile).
//
// 15s, not 5s. The old budget was set when CallTool re-handshook on EVERY
// lookup, so it had to cover initialize + tools/call back-to-back. Measured
// live against the deployed stack: initialize 0.5s-2.7s plus a 0.9s-3.3s
// tools/call totalled 4997ms against the 5000ms deadline. It lost by 3ms and
// the GitLab project-canonicalization gate failed closed on every non-numeric
// project_id spelling. Every live-resolved gate shares this constant, so they
// all sat on the same knife edge.
//
// upstream.Client now reuses its MCP session, so the steady-state lookup is
// ONE round trip and typically finishes in ~1-3s. The budget is nonetheless
// kept at 15s rather than tightened back down, because it still has to cover
// the cold cases that legitimately need two round trips: the first lookup
// after a shim restart, and the one-shot re-handshake when a cached session
// has been evicted upstream. Tightening it to the happy-path figure would
// re-arm exactly the failure this change set exists to remove. Still far
// below the gateway's own timeout, so a genuinely hung upstream is cut off
// here rather than by the gateway.
const upstreamLookupTimeout = 15 * time.Second

// moderationTimeout bounds a single moderation-endpoint call (fails open on
// timeout -- see checkModeration).
const moderationTimeout = 10 * time.Second

// callToolMeta is the vMCP optimizer's (thv vmcp serve --optimizer/--optimizer-embedding)
// meta-tool name. With the optimizer on, vMCP exposes only find_tool/call_tool instead
// of the real backend tools, so every actual invocation arrives wrapped as
// call_tool{tool_name, parameters} rather than under its own name. Left unhandled, the
// mapping lookup below would only ever see "call_tool" — never a mapped tool — and
// silently pass every call through on this backend's defaultAction: allow. Field names
// match github.com/stacklok/toolhive/pkg/vmcp/optimizer.CallToolInput.
const callToolMeta = "call_tool"

// denyMessage is the fallback used when Cerbos denies a call but the matched
// deny rule carries no policy `output` (see
// charts/cerbos-policies/policies/*.yaml `output:` blocks). It intentionally
// omits resource/action to avoid leaking probed
// state; detail goes to the shim log. Prefer adding an `output` to the rule
// over relying on this generic string: without a specific
// reason, a calling agent has no way to distinguish "try a different
// approach" (self-approve blocked, use REQUEST_CHANGES instead) from
// "this whole avenue is closed" (protected branch, wrong project), and burns
// retries rediscovering the boundary by trial and error.
const denyMessage = "Access denied by security policy. This is an intentional restriction, not a tool error; try a different resource or action."

// Principal is audit metadata (not an authz control; policy denies only by resource).
type Principal struct {
	ID    string
	Roles []string
}

// AuditPrincipalID is the fixed identity stamped on every Cerbos request.
const AuditPrincipalID = "agent"

// AuditPrincipal returns the fixed identity stamped on every Cerbos request.
func AuditPrincipal() Principal {
	return Principal{ID: AuditPrincipalID, Roles: []string{"agent"}}
}

// Server implements pb.ExtMcpServer.
type Server struct {
	pb.UnimplementedExtMcpServer
	mapping   *config.Mapping
	engine    *eval.Engine
	decider   authz.Decider
	principal Principal

	// selfToken is the shim's secret self-identifier. The shim's internal MCP
	// client stamps it on re-entrant lookups; CheckRequest verifies it in
	// constant time before admitting vmcp-internal. Empty fails closed.
	selfToken string

	// notionAncestry, when set, gates every existing-page Notion write
	// (update-page, create-comment) to pages under one of
	// notionAllowedParentIDs via a live notion-fetch lookup — a network round
	// trip the CEL/Cerbos path can't make (it's pure/synchronous, no I/O). It
	// lives on Server rather than in a CEL helper for that reason.
	// notionAllowedParentIDs is a caller-scoped allowlist of parent folders
	// (e.g. Scratchpad plus a set of team folders — HAH's multi-parent
	// scoping); a page passes the gate if it descends from ANY of them.
	// notion-create-pages is NOT covered by this list — it stays pinned to
	// Scratchpad-only via its own, narrower Cerbos deny rule.
	notionAncestry         upstream.ToolCaller
	notionAllowedParentIDs []string

	// linearIssueTeam, when set, resolves a Linear issueId/id to its current
	// team via a live linear_get_issue lookup -- a network round trip the
	// CEL/Cerbos path can't make, same rationale as notionAncestry above.
	// Used by: save_comment (always), and save_issue UPDATE calls
	// that omit an explicit `team` arg (an explicit team is
	// already resolved directly by linearIssueAttr and never re-looked-up
	// here). Unlike the Notion gate this doesn't deny directly: it injects
	// the resolved team into the resource's teamId attr so Cerbos's existing
	// deny-non-devops-team rule (resource_linear.yaml) evaluates it exactly
	// like an explicit-team call, with zero duplication of the allowlist.
	linearIssueTeam upstream.ToolCaller

	// linearProjectTeam, when set, resolves a Linear project id to its
	// CURRENT team(s) via a live linear_get_project lookup, same rationale
	// as linearIssueTeam above. Used only by save_project UPDATE calls that
	// set neither addTeams nor setTeams -- a call that sets either
	// is already resolved directly by linearProjectAttr and never
	// re-looked-up here. Injects the resolved teams into the resource's
	// teams attr so Cerbos's existing deny-non-devops-project-teams rule
	// evaluates it exactly like an explicit addTeams/setTeams call.
	linearProjectTeam upstream.ToolCaller

	// pagerdutyIncidentService, when set, resolves EVERY incident id a
	// manage_incidents/add_note_to_incident call targets to its owning
	// service via a live pagerduty_get_incident lookup -- neither
	// tool's own args carry a service/team identifier at all, only an
	// opaque incident_id/incident_ids, so there is nothing for a CEL helper
	// to check without this network round trip, same rationale as
	// notionAncestry/linearIssueTeam above. Injects the resolved service
	// id(s) into the resource's serviceIds attr so Cerbos's
	// deny-write-outside-allowed-services rule (resource_pagerduty.yaml)
	// evaluates it exactly like an explicit-service call.
	pagerdutyIncidentService upstream.ToolCaller

	// githubPRAuthor, when set, resolves owner/repo/pullNumber to a pull
	// request's real author via a live pull_request_read lookup -- a
	// network round trip the CEL/Cerbos path can't make, same rationale as
	// linearIssueTeam above. Used by every tool that writes to an EXISTING
	// PR (githubExistingPRTools); create_pull_request has no prior PR to
	// check. Injects the resolved login into the resource's prAuthor attr
	// so Cerbos's deny-not-own-pr rule (resource_github.yaml) evaluates it
	// against ${githubUsername}.
	githubPRAuthor upstream.ToolCaller

	// gitlabMRAuthor, when set, resolves a project_id + merge_request_iid (or
	// source_branch) to a merge request's real author via a live
	// get_merge_request lookup -- a network round trip the CEL/Cerbos path
	// can't make, same rationale as githubPRAuthor above. Used by every tool
	// that writes to an EXISTING MR's own fields (gitlabExistingMRTools);
	// create_merge_request has no prior MR to check. Injects the resolved
	// username into the resource's mrAuthor attr so Cerbos's deny-not-own-mr
	// rule (resource_gitlab.yaml) evaluates it against ${gitlabUsername}.
	gitlabMRAuthor upstream.ToolCaller

	// gitlabProjectCanonicalizer, when set, resolves any spelling of a GitLab
	// project (numeric id, group/project path, percent-encoded path, or any
	// casing of either path form -- all four verified live) to the single
	// numeric id that project actually has, via a live get_project lookup.
	// Unlike the ownership gates above this resolves an IDENTITY rather than
	// an owner: without it ${gitlabAllowedProjects} is compared against
	// project_id exactly as sent, so the same project named a different but
	// equally valid way misses the allowlist and is denied -- fail-closed, but
	// a false deny on legitimate work that the agent cannot diagnose. Injects
	// the resolved id as the projectId attr Cerbos's deny-non-allowed-project
	// rule reads, so an operator lists ONE canonical value per project instead
	// of guessing every spelling agents might send.
	gitlabProjectCanonicalizer upstream.ToolCaller

	// jiraIssueAssignee, when set, resolves a Jira issue key to its CURRENT
	// assignee via a live jira_jira_get_issue lookup -- a network round trip
	// the CEL/Cerbos path can't make, same rationale as linearIssueTeam
	// above. Used by jiraAssigneeGatedTools when the call itself carries no
	// verifiable assignee signal (jiraFieldsAttr always populates an
	// assignee key, possibly empty). Injects the resolved assignee into the
	// resource's assignee attr so Cerbos's existing
	// deny-assignee-outside-allowed rule (resource_jira.yaml) evaluates it
	// against ${jiraAllowedAssignees}.
	jiraIssueAssignee upstream.ToolCaller

	// notionPageAuthor, when set, resolves a Notion page id to whether it was
	// created by notionOperatorUserID via a live notion-fetch+notion-search
	// lookup -- a network round trip the CEL/Cerbos path can't make, same
	// rationale as linearIssueTeam above. Used by notionAncestryGatedActions
	// (update/comment on an EXISTING page; create has no prior-ownership
	// question). Injects a pageAuthorMismatch=true attr (only on mismatch, so
	// the no-signal case stays unset) so Cerbos's deny-not-own-page rule
	// (resource_notion.yaml) evaluates it -- same inject-then-Cerbos-decides
	// pattern as the other gates above, not a second direct-deny like the
	// ancestry check this gate sits alongside.
	notionPageAuthor upstream.ToolCaller

	// notionOperatorUserID is the operator's own Notion user id
	// (${notionUserId}), passed to notionPageAuthor's lookup as the identity
	// a page's authorship is checked against.
	notionOperatorUserID string

	// alertmanagerSilenceOwner, when set, resolves a silenceId to its real
	// createdBy via a live getSilences lookup -- a network round trip the
	// CEL/Cerbos path can't make, same rationale as githubPRAuthor above.
	// Used by alertmanagerDeleteSilenceTools. Injects the resolved createdBy
	// into the resource's createdBy attr so Cerbos's deny-not-own-silence rule
	// (resource_alertmanager.yaml) evaluates it against
	// ${alertmanagerCreatedBy} -- the same value mapping.yaml's `force` stamps
	// onto every createSilence call, so the two halves stay in sync by
	// construction.
	alertmanagerSilenceOwner upstream.ToolCaller

	// moderationChecker, when set, sends free-text args of matching write
	// calls through OpenAI's Moderations endpoint before the call reaches
	// vMCP. Nil disables the gate (per-cluster toggle, see main.go). Unlike
	// the redaction gate, a flagged result DENIES the call -- there's no
	// safe partial-mutation of "the content is offensive."
	moderationChecker moderation.Checker

	// moderatedWriteVerbs is the verb list isModeratedWriteTool checks against.
	moderatedWriteVerbs []string

	// promptInjectionDetector, when set, runs stage 1 (broad regex prefilter)
	// of the two-stage prompt-injection gate (HAH-107) over every
	// redactableResponseMethods response body (tools/call, resources/read,
	// prompts/get -- the same set secret redaction already runs on). Nil
	// disables the gate (per-cluster toggle, see main.go).
	promptInjectionDetector promptinjection.Detector

	// promptInjectionJudge, when set, runs stage 2 (LLM-judge confirmation)
	// ONLY on text that already matched stage 1 -- this is the cost-control
	// mechanism, most reads never reach it. A confirmed ("yes") judgment
	// DENIES the call (see checkPromptInjection) -- unlike the old log-only
	// v1, this is now a blocking gate, made safe to enforce specifically
	// BECAUSE stage 2 filters stage 1's deliberately noisy matches down to
	// a confirmed detection. Nil (gate enabled but no judge configured)
	// degrades to stage-1-match-always-fails-open, same as a judge-service
	// error -- see checkPromptInjection's doc comment.
	promptInjectionJudge promptinjection.Judge
}

// Option configures a Server at construction. Variadic so existing four-arg
// New callers (tests, and any backend that doesn't need the ancestry gate)
// keep compiling unchanged.
type Option func(*Server)

// WithNotionAncestry enables the Notion existing-page-write ancestry gate.
// client resolves a page's ancestors (production: an upstream.Client to vMCP;
// tests: a stub); allowedParentIDs is the set of parent folders a page must
// descend from ANY one of to pass (Scratchpad plus any additional team
// folders the caller configures — the caller is responsible for including
// Scratchpad in this list if it should remain allowed).
func WithNotionAncestry(client upstream.ToolCaller, allowedParentIDs []string) Option {
	return func(s *Server) {
		s.notionAncestry = client
		s.notionAllowedParentIDs = allowedParentIDs
	}
}

// WithLinearIssueTeam enables the Linear issue team-resolution gate: always
// for save_comment, and for save_issue UPDATE calls that omit an
// explicit `team` arg. client resolves an issue id to its current
// team (production: an upstream.Client to vMCP; tests: a stub).
func WithLinearIssueTeam(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.linearIssueTeam = client
	}
}

// WithLinearProjectTeam enables the Linear save_project UPDATE team-
// resolution gate: fires only when the call sets neither addTeams
// nor setTeams. client resolves a project id to its current team(s)
// (production: an upstream.Client to vMCP; tests: a stub).
func WithLinearProjectTeam(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.linearProjectTeam = client
	}
}

// WithPagerdutyIncidentService enables the PagerDuty incident service-
// resolution gate: every manage_incidents/add_note_to_incident
// call has each targeted incident id resolved to its owning service via a
// live lookup. client resolves an incident id to its service id
// (production: an upstream.Client to vMCP; tests: a stub).
func WithPagerdutyIncidentService(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.pagerdutyIncidentService = client
	}
}

// WithGithubPRAuthor enables the GitHub existing-PR-write author-resolution
// gate: every update_pull_request/update_pull_request_branch/
// request_copilot_review call has its target PR's real author resolved via
// a live lookup. client resolves owner/repo/pullNumber to the PR's author
// login (production: an upstream.Client to vMCP; tests: a stub).
func WithGithubPRAuthor(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.githubPRAuthor = client
	}
}

// WithGitlabMRAuthor enables the GitLab existing-MR-write author-resolution
// gate: every update_merge_request call has its target MR's real author
// resolved via a live lookup. client resolves project_id + merge_request_iid
// (or source_branch) to the MR's author username (production: an
// upstream.Client to vMCP; tests: a stub). No identity parameter needed -- the
// comparison value lives entirely in Cerbos's ${gitlabUsername}, same shape as
// WithGithubPRAuthor.
func WithGitlabMRAuthor(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.gitlabMRAuthor = client
	}
}

// WithGitlabProjectCanonicalizer enables the GitLab project-canonicalization
// gate: every mapped GitLab call has its project_id resolved to the project's
// numeric id via a live get_project lookup before Cerbos evaluates the
// allowlist. client resolves any accepted spelling to that id (production: an
// upstream.Client to vMCP; tests: a stub). Without it the allowlist is matched
// against the raw argument, so an operator must enumerate every spelling
// agents might send; with it they list one canonical id per project.
func WithGitlabProjectCanonicalizer(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.gitlabProjectCanonicalizer = client
	}
}

// WithJiraIssueAssignee enables the Jira ticket-assignee resolution gate:
// update_issue/add_comment/transition_issue calls that don't themselves
// carry a verifiable assignee signal have the issue's CURRENT assignee
// resolved via a live lookup. client resolves an issue key to its current
// assignee (production: an upstream.Client to vMCP; tests: a stub).
func WithJiraIssueAssignee(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.jiraIssueAssignee = client
	}
}

// WithNotionPageAuthor enables the Notion existing-page-write author-
// resolution gate: update-page/create-comment calls have their target
// page's real author resolved via a live lookup and compared against
// operatorUserID. client resolves a page id to whether it was authored by
// operatorUserID (production: an upstream.Client to vMCP; tests: a stub).
func WithNotionPageAuthor(client upstream.ToolCaller, operatorUserID string) Option {
	return func(s *Server) {
		s.notionPageAuthor = client
		s.notionOperatorUserID = operatorUserID
	}
}

// WithAlertmanagerSilenceOwner enables the Alertmanager deleteSilence
// owner-resolution gate: every deleteSilence call has its target silence's
// real createdBy resolved via a live lookup. client resolves a silenceId to
// its createdBy (production: an upstream.Client to vMCP; tests: a stub). No
// identity parameter needed -- the comparison value lives entirely in
// Cerbos's ${alertmanagerCreatedBy}, same shape as WithGithubPRAuthor.
func WithAlertmanagerSilenceOwner(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.alertmanagerSilenceOwner = client
	}
}

// WithModeration enables the outbound content-moderation gate using checker
// to classify free-text arguments. Unset (nil checker) disables the gate.
func WithModeration(checker moderation.Checker) Option {
	return func(s *Server) {
		s.moderationChecker = checker
		if s.moderatedWriteVerbs == nil {
			s.moderatedWriteVerbs = DefaultModeratedWriteVerbs
		}
	}
}

// WithModerationVerbs overrides the default write-verb list. Apply after
// WithModeration so it isn't overwritten by WithModeration's own default.
func WithModerationVerbs(verbs []string) Option {
	return func(s *Server) {
		if len(verbs) > 0 {
			s.moderatedWriteVerbs = verbs
		}
	}
}

// WithPromptInjectionDetection enables the two-stage response-side
// prompt-injection gate (HAH-107): detector runs the stage-1 regex
// prefilter over matching response bodies, and judge (may be nil, though
// callers should always pass one when enabling the gate -- see main.go)
// runs stage-2 LLM-judge confirmation only on text stage 1 already
// flagged. A confirmed judge verdict DENIES the call -- see
// checkPromptInjection's doc comment for the full fail-open/deny contract.
// Unset (nil detector) disables the gate entirely.
func WithPromptInjectionDetection(detector promptinjection.Detector, judge promptinjection.Judge) Option {
	return func(s *Server) {
		s.promptInjectionDetector = detector
		s.promptInjectionJudge = judge
	}
}

// WithSelfToken sets the shim's secret self-identifier (see Server.selfToken).
// The same value is handed to the upstream.Client via upstream.WithSelfToken so
// the shim's own re-entrant lookups carry it and CheckRequest can authenticate
// them on the vmcp-internal backend. Empty token leaves that backend's token
// check inert (fail-safe -- see the field doc).
func WithSelfToken(token string) Option {
	return func(s *Server) { s.selfToken = token }
}

// New constructs a Server. The engine must already be compiled from mapping.
func New(m *config.Mapping, e *eval.Engine, d authz.Decider, p Principal, opts ...Option) *Server {
	s := &Server{mapping: m, engine: e, decider: d, principal: p}
	for _, o := range opts {
		o(s)
	}
	return s
}

// callParams is the tools/call params shape (rmcp CallToolRequestParam).
type callParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

// CheckRequest is the pre-forward gate. It returns Pass{} to allow, Mutated{}
// to allow-with-rewritten-args (only for a tool carrying a mapping `force`
// set), or an AuthorizationError to deny. It never sets metadata or
// header_mutation.
func (s *Server) CheckRequest(ctx context.Context, req *pb.McpRequest) (*pb.McpRequestResult, error) {
	// The backend is reserved for the shim by two independent locks: a
	// CiliumNetworkPolicy restricts its :81 listener to the shim pod (network),
	// and the caller must present the configured self-token in the
	// SelfHeaderName header (constant-time). This branch runs before
	// resolveBackend because vmcp-internal has no mapping entry.
	if isInternalBackend(req.GetServiceNames()) {
		if !s.isSelfRequest(req) {
			log.Printf("deny: tokenless caller on the reserved vmcp-internal backend (method=%q backend=%v)", req.GetMethod(), req.GetServiceNames())
			return deny("the vmcp-internal backend is reserved for the cerbos shim's own re-entrant lookups"), nil
		}
		if !internalAllowedRequestMethods[req.GetMethod()] {
			log.Printf("deny: unsupported method on the reserved vmcp-internal backend (method=%q backend=%v)", req.GetMethod(), req.GetServiceNames())
			return deny(fmt.Sprintf("method %q is not permitted on the vmcp-internal backend", req.GetMethod())), nil
		}
		return pass(), nil
	}

	backend, derr := s.resolveBackend(req.GetServiceNames())
	if derr != nil {
		return deny(derr.Error()), nil
	}

	b := s.mapping.Backends[backend]

	// Cerbos authz is only evaluated for tools/call -- no mapping entry
	// exists to build a Cerbos resource from a resources/read URI or a
	// prompts/get name (see resourcesRead/promptsGet comment above), so
	// there's nothing to check for either. Both still get their RESPONSE
	// bodies scrubbed by CheckResponse (redactableResponseMethods); this
	// gate only concerns request-side authz + argument redaction.
	if req.GetMethod() != toolsCall {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("method %q not handled on deny-default backend %q", req.GetMethod(), backend)), nil
		}
		return pass(), nil
	}

	// Unparseable/missing params deny; don't rely on gateway FailClosed for our own failures.
	raw := req.GetMcpRequest()
	if len(raw) == 0 {
		return deny("tools/call has no params"), nil
	}
	var cp callParams
	if err := json.Unmarshal(raw, &cp); err != nil {
		return deny(fmt.Sprintf("unparseable tools/call params: %v", err)), nil
	}
	if cp.Name == "" {
		return deny("tools/call params missing tool name"), nil
	}
	if cp.Arguments == nil {
		cp.Arguments = map[string]any{} // valid: some tools take no args
	}

	// wrapped remembers whether this call arrived through the optimizer's
	// call_tool meta-tool, so an eventual mutation can be re-wrapped into the
	// same shape before forwarding (the gateway replaces the whole params
	// object verbatim; it does not know about call_tool itself).
	wrapped := cp.Name == callToolMeta
	if wrapped {
		toolName, ok := cp.Arguments["tool_name"].(string)
		if !ok || toolName == "" {
			return deny("call_tool missing string tool_name"), nil
		}
		params, _ := cp.Arguments["parameters"].(map[string]any) // absent/wrong-type -> no args
		cp.Name = toolName
		cp.Arguments = params
		if cp.Arguments == nil {
			cp.Arguments = map[string]any{}
		}
	}

	// Content-moderation gate: runs before the mapping lookup below and
	// before Cerbos, on the tool name/args alone -- deliberately independent
	// of whether cp.Name has a Cerbos mapping entry, so an entirely unmapped
	// backend (e.g. GitLab, which has no Cerbos policy at all) still gets
	// its free-text writes checked. No-op if disabled or the tool doesn't
	// match a write verb.
	if s.moderationChecker != nil && isModeratedWriteTool(cp.Name, s.moderatedWriteVerbs) {
		if derr := s.checkModeration(ctx, cp.Arguments); derr != nil {
			log.Printf("deny: %s failed content moderation (backend=%s): %v", cp.Name, backend, derr)
			return deny(derr.Error()), nil
		}
	}

	if _, ok := b.Tools[cp.Name]; !ok {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("tool %q not mapped on deny-default backend %q", cp.Name, backend)), nil
		}
		return pass(), nil
	}

	res, err := s.engine.Eval(eval.CallInput{
		Tool: cp.Name, Backend: backend, Method: req.GetMethod(), Args: cp.Arguments,
	})
	if err != nil {
		return deny(fmt.Sprintf("policy input eval: %v", err)), nil
	}

	// Notion existing-page-write gate: this runs BEFORE Cerbos (a live
	// ancestry lookup Cerbos itself can't do) and denies any write to an
	// existing page (update-page, create-comment) outside the allowed parent
	// folders. The Cerbos policy still independently blocks destructive
	// update-page commands (replace_content / allow_deleting_content) on the
	// pages that DO pass this gate; the two checks are complementary, not
	// redundant.
	if res.ResourceType == notionPageResource && notionAncestryGatedActions[res.Action] {
		if derr := s.checkNotionAncestry(ctx, res.ID); derr != nil {
			log.Printf("deny: notion %s ancestry (page=%q backend=%s): %v", res.Action, res.ID, backend, derr)
			return deny(derr.Error()), nil
		}
	}

	// Notion existing-page-write author-resolution gate: unlike the ancestry
	// gate above, this doesn't deny directly -- it resolves the page's real
	// author via a live lookup and, only on a mismatch, injects
	// pageAuthorMismatch=true into res.Attr, so Cerbos's deny-not-own-page
	// rule (resource_notion.yaml) evaluates it exactly like the other
	// inject-then-Cerbos-decides gates (GitHub prAuthor, Jira/Linear
	// assignee). A lookup failure still fails closed here (deny), since an
	// unverifiable author is not the same as a verified non-mismatch.
	if res.ResourceType == notionPageResource && notionAncestryGatedActions[res.Action] {
		mismatch, derr := s.checkNotionPageAuthor(ctx, res.ID)
		if derr != nil {
			log.Printf("deny: notion %s author lookup (page=%q backend=%s): %v", res.Action, res.ID, backend, derr)
			return deny(derr.Error()), nil
		}
		if mismatch {
			res.Attr["pageAuthorMismatch"] = true
		}
	}

	// Linear save_comment team-resolution gate: this runs BEFORE
	// Cerbos and, unlike the Notion gate above, doesn't deny directly -- it
	// resolves issueId to its team via a live lookup and injects that team
	// into res.Attr's teamId key, so Cerbos's existing deny-non-devops-team
	// rule (resource_linear.yaml) evaluates this exactly like a save_issue
	// call. save_issue itself is untouched (its own teamId is already
	// populated by linearIssueAttr in mapping.yaml); this only fires for
	// linear_save_comment, and only when the call has an issueId to resolve
	// (a comment on a project/initiative/document/milestone, or a reply via
	// parentId with no entity ref, has nothing to resolve and passes
	// unchecked -- same fail-open-when-unverifiable posture as save_project's
	// linearProjectAttr helper). Gated on the issueId ATTR, not res.ID --
	// res.ID falls back to "*" when issueId is absent (mapping.yaml), same
	// non-empty-id convention save_project's id: get(args,'id','*') uses,
	// since Cerbos itself rejects an empty resource.id before policy ever
	// runs.
	// The same lookup also resolves the issue's CURRENT assignee, injected
	// into this same res.Attr's assignee key so Cerbos's existing
	// deny-assignee-outside-allowed rule (resource_linear.yaml) evaluates a
	// comment on someone else's issue exactly like an explicit-assignee
	// save_issue call -- closes the analogous gap for assignee scoping: a
	// comment (or a plain field edit, below) previously carried no assignee
	// attr at all regardless of who the issue was really assigned to.
	// assignee may legitimately resolve to "" (a real issue can have no
	// assignee) -- only fails closed on a genuine lookup/shape failure, see
	// upstream.GetIssueDetails's contract. assigneeVerified marks that this
	// gate actually resolved a definitive current assignee (even "none"), so
	// deny-write-unassigned-issue (resource_linear.yaml) can deny a comment
	// on a genuinely unassigned issue -- previously that case left the
	// assignee attr empty/absent and passed unchecked, same gap the Jira
	// gate had.
	if cp.Name == linearSaveCommentTool && res.ResourceType == linearTeamResource {
		issueID, _ := res.Attr["issueId"].(string)
		if issueID != "" {
			team, assignee, derr := s.checkLinearIssueTeam(ctx, issueID)
			if derr != nil {
				log.Printf("deny: linear save_comment team/assignee lookup (issue=%q backend=%s): %v", issueID, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["teamId"] = team
			res.Attr["assigneeVerified"] = true
			if assignee != "" {
				res.Attr["assignee"] = assignee
			}
		}
	}

	// Linear save_issue UPDATE team/assignee-resolution gate: closes the gap
	// where a plain field edit on an existing issue (no `team`/`assignee` arg
	// at all) fell through to allow-all regardless of the issue's REAL team
	// or CURRENT assignee, since linearIssueAttr only surfaces teamId/assignee
	// when the call itself sets them. Fires only when: (a) this is
	// save_issue, (b) the call is an update (has an `id` arg -- res.ID is
	// that same id per mapping.yaml's `id: get(args,'id', get(args,'team',''))`),
	// and (c) attr is missing EITHER teamId or assignee, meaning the call
	// didn't set that field itself (an explicit team/assignee, create or
	// update, is linearIssueAttr's own directly-verifiable signal and must
	// never be overridden by a lookup here) -- team and assignee are resolved
	// independently from the SAME single lookup, each only overwriting the
	// key it was missing. A create call always sets `team` (required) and
	// has no `id`, so it never reaches this branch regardless of whether it
	// set assignee (that gap is closed separately by
	// deny-create-missing-assignee's own arg-based check).
	//
	// assigneeVerified marks that this gate actually resolved a definitive
	// current assignee for THIS call (even "none") -- only set when
	// !hasAssignee, i.e. only when the call itself didn't already supply a
	// directly-verifiable assignee signal. deny-write-unassigned-issue
	// (resource_linear.yaml) uses it to deny a plain field edit on a
	// genuinely unassigned issue, closing the same gap the Jira gate had:
	// previously such an update left the assignee attr empty/absent and
	// passed unchecked.
	if cp.Name == linearSaveIssueTool && res.ResourceType == linearTeamResource {
		_, hasTeam := res.Attr["teamId"]
		_, hasAssignee := res.Attr["assignee"]
		if !hasTeam || !hasAssignee {
			if issueID, _ := cp.Arguments["id"].(string); issueID != "" {
				team, assignee, derr := s.checkLinearIssueTeam(ctx, issueID)
				if derr != nil {
					log.Printf("deny: linear save_issue update team/assignee lookup (issue=%q backend=%s): %v", issueID, backend, derr)
					return deny(derr.Error()), nil
				}
				if !hasTeam {
					res.Attr["teamId"] = team
				}
				if !hasAssignee {
					res.Attr["assigneeVerified"] = true
					if assignee != "" {
						res.Attr["assignee"] = assignee
					}
				}
			}
		}
	}

	// Linear save_project UPDATE team-resolution gate: same shape
	// as the save_issue gate above -- closes the gap where a plain project
	// field edit (no addTeams/setTeams) fell through to allow-all regardless
	// of the project's REAL team(s), since linearProjectAttr only surfaces
	// a `teams` attr when the call itself sets one of those args. Fires only
	// when: (a) this is save_project, (b) the call is an update (has an `id`
	// arg), and (c) attr has NO teams key, meaning neither addTeams nor
	// setTeams was set (an explicit reassignment is linearProjectAttr's own
	// directly-verifiable signal and must never be overridden here). A
	// create call always sets one of addTeams/setTeams (Linear requires at
	// least one team on project creation) and has no `id`, so it never
	// reaches this branch.
	if cp.Name == linearSaveProjectTool && res.ResourceType == linearTeamResource {
		if _, hasTeams := res.Attr["teams"]; !hasTeams {
			if projectID, _ := cp.Arguments["id"].(string); projectID != "" {
				teams, derr := s.checkLinearProjectTeam(ctx, projectID)
				if derr != nil {
					log.Printf("deny: linear save_project update team lookup (project=%q backend=%s): %v", projectID, backend, derr)
					return deny(derr.Error()), nil
				}
				res.Attr["teams"] = teams
			}
		}
	}

	// PagerDuty incident service-resolution gate: this runs BEFORE
	// Cerbos and, like the Linear team gates above, doesn't deny directly --
	// it resolves every incident id the call targets to its owning service
	// via a live lookup and injects the resolved service id(s) into the
	// resource's serviceIds attr, so Cerbos's deny-write-outside-allowed-
	// services rule (resource_pagerduty.yaml) evaluates it exactly like an
	// explicit-service call. manage_incidents carries incident_ids (an
	// array, since it's a bulk-update tool); add_note_to_incident carries a
	// single incident_id. Both are handled the same way: resolve every
	// non-empty id, fail closed on ANY lookup error (a partially-resolved
	// batch is not a safe signal to check against an allowlist).
	getIncidentTool, pagerdutyGated := pagerdutyManageIncidentsTools[cp.Name]
	if !pagerdutyGated {
		getIncidentTool, pagerdutyGated = pagerdutyAddNoteTools[cp.Name]
	}
	if res.ResourceType == pagerdutyIncidentResource && pagerdutyGated {
		incidentIDs := pagerdutyIncidentIDsFromArgs(cp.Name, cp.Arguments)
		if len(incidentIDs) > 0 {
			serviceIDs, derr := s.checkPagerdutyIncidentServices(ctx, getIncidentTool, incidentIDs)
			if derr != nil {
				log.Printf("deny: pagerduty %s service lookup (incidents=%v backend=%s): %v", cp.Name, incidentIDs, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["serviceIds"] = serviceIDs
		}
	}

	// GitHub PR-author gate: this runs BEFORE Cerbos and, like the Linear/
	// PagerDuty gates above, doesn't deny directly -- it resolves the target
	// PR's real author via a live pull_request_read lookup and injects it
	// into the resource's prAuthor attr, so Cerbos's deny-not-own-pr rule
	// (resource_github.yaml) can catch a hallucinated/wrong PR number even
	// though nothing in these tools' own arguments ever names an "author" to
	// check directly (a PR's author isn't reassignable via any of them).
	// Only fires for tools that target an EXISTING PR by pullNumber;
	// owner/repo/pullNumber are all required args on these tools, so a call
	// missing any of them is already malformed and the gate is skipped
	// (same "nothing verifiable, nothing to inject" posture as the Linear
	// gates' own missing-id case) rather than specially fail-closed here.
	// A pullNumber that IS present but isn't a usable number is different:
	// skipping it would leave prAuthor unset and let the call past
	// deny-not-own-pr, so that denies instead.
	if githubExistingPRTools[cp.Name] && res.ResourceType == githubRepoResource {
		owner, _ := cp.Arguments["owner"].(string)
		repo, _ := cp.Arguments["repo"].(string)
		raw, present := cp.Arguments["pullNumber"]
		pullNumber, ok := coerceNumber(raw)
		if present && !ok {
			log.Printf("deny: %s unusable pullNumber %#v (repo=%s/%s backend=%s)", cp.Name, raw, owner, repo, backend)
			return deny("could not read this GitHub PR number, so its author cannot be verified (failing closed)"), nil
		}
		if owner != "" && repo != "" && ok {
			author, derr := s.checkGithubPRAuthor(ctx, owner, repo, pullNumber)
			if derr != nil {
				log.Printf("deny: %s PR author lookup (repo=%s/%s pr=%v backend=%s): %v", cp.Name, owner, repo, pullNumber, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["prAuthor"] = author
		}
	}

	// GitLab project-canonicalization gate: runs BEFORE Cerbos and before the
	// MR-author gate below, and unlike the ownership gates it resolves the
	// resource's IDENTITY rather than its owner. GitLab accepts a project
	// named four different ways -- numeric id, group/project path, the
	// percent-encoded path, and any casing of either path form (all four
	// verified against the live instance to hit the same project) -- but
	// deny-non-allowed-project compares ${gitlabAllowedProjects} against
	// project_id exactly as sent. Without this, the SAME allowlisted project
	// named a different but equally valid way misses the list and is denied:
	// fail-closed, but a false deny on legitimate work with a message
	// ("outside the allowed project list") that actively misleads, and no way
	// for the agent to know it should retry with another spelling. Resolving
	// to the numeric id here means an operator lists ONE value per project.
	//
	// Applies to both project attrs: projectId, and targetProjectId (the
	// second project create_issue_link/create_merge_request carry), which the
	// same Cerbos rule checks against the same allowlist and so needs the same
	// canonical form. Fails closed on lookup error -- an unresolvable project
	// is not an allowlisted one. An already-numeric value short-circuits with
	// no network call, so the common case costs nothing.
	if res.ResourceType == gitlabProjectResource {
		for _, key := range []string{"projectId", "targetProjectId"} {
			raw, _ := res.Attr[key].(string)
			if raw == "" {
				continue
			}
			canonical, derr := s.checkGitlabProjectCanonical(ctx, raw)
			if derr != nil {
				log.Printf("deny: %s project canonicalization (%s=%q backend=%s): %v", cp.Name, key, raw, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr[key] = canonical
		}
	}

	// GitLab MR-author gate: the GitLab counterpart of the GitHub gate above,
	// same inject-then-Cerbos-decides shape -- it resolves the target merge
	// request's real author via a live get_merge_request lookup and injects it
	// into the resource's mrAuthor attr, so Cerbos's deny-not-own-mr rule
	// (resource_gitlab.yaml) can catch a hallucinated/wrong merge_request_iid
	// even though nothing in update_merge_request's own arguments ever names an
	// "author" to check directly. The MR is selected exactly as the gated call
	// selected it: by merge_request_iid when given, else by source_branch
	// (update_merge_request accepts either, and get_merge_request takes both the
	// same way), so the lookup can't resolve a DIFFERENT merge request than the
	// one about to be written to. A call carrying no project_id or neither
	// selector is already malformed and the gate is skipped rather than
	// specially fail-closed here -- same "nothing verifiable, nothing to
	// inject" posture as the GitHub gate's own missing-arg case. scalarArg
	// (not a bare string assertion) because these are declared string but a
	// caller naming a project/MR by number sends a JSON number, the same
	// silent-miss the gitlabProjectAttr CEL helper guards against.
	if gitlabExistingMRTools[cp.Name] && res.ResourceType == gitlabProjectResource {
		projectID := scalarArg(cp.Arguments, "project_id")
		mrIID := scalarArg(cp.Arguments, "merge_request_iid")
		sourceBranch := scalarArg(cp.Arguments, "source_branch")
		if projectID != "" && (mrIID != "" || sourceBranch != "") {
			author, derr := s.checkGitlabMRAuthor(ctx, projectID, mrIID, sourceBranch)
			if derr != nil {
				log.Printf("deny: %s MR author lookup (project=%s iid=%q source_branch=%q backend=%s): %v", cp.Name, projectID, mrIID, sourceBranch, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["mrAuthor"] = author
		}
	}

	// Alertmanager silence-owner resolution gate: this runs BEFORE Cerbos
	// and, like the GitHub PR-author gate above, doesn't deny directly -- it
	// resolves the target silence's real createdBy via a live getSilences
	// lookup and injects it into the resource's createdBy attr, so Cerbos's
	// deny-not-own-silence rule (resource_alertmanager.yaml) can catch a
	// hallucinated/wrong silenceId even though deleteSilence's own args
	// never carry an "owner" to check directly. silenceId is a required arg
	// on this tool; a call missing it is already malformed and the gate is
	// skipped (same "nothing verifiable, nothing to inject" posture as the
	// GitHub gate's own missing-arg case).
	if getSilencesTool, ok := alertmanagerDeleteSilenceTools[cp.Name]; ok && res.ResourceType == alertmanagerSilenceResource {
		if silenceID, _ := cp.Arguments["silenceId"].(string); silenceID != "" {
			createdBy, derr := s.checkAlertmanagerSilenceOwner(ctx, getSilencesTool, silenceID)
			if derr != nil {
				log.Printf("deny: %s silence owner lookup (silence=%s backend=%s): %v", cp.Name, silenceID, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["createdBy"] = createdBy
		}
	}

	// Jira ticket-assignee resolution gate: this runs BEFORE Cerbos and,
	// like the Linear team/assignee gates above, doesn't deny directly -- it
	// resolves the issue's CURRENT assignee via a live lookup and injects it
	// into the resource's assignee attr, so Cerbos's existing
	// deny-assignee-outside-allowed rule (resource_jira.yaml) evaluates a
	// plain field edit/comment/transition on someone else's issue exactly
	// like an explicit-assignee create_issue call. jiraFieldsAttr already
	// populates an assignee key on every jira_project write (possibly
	// empty), so an empty value reliably means "this call carries no
	// verifiable assignee signal of its own" -- add_comment/
	// transition_issue never carry one at all (no fields/additional_fields
	// arg to smuggle it in), update_issue only when it doesn't touch
	// assignee itself.
	//
	// assigneeVerified marks that THIS gate actually ran and got a
	// definitive answer, even when that answer is "unassigned"
	// (resolved == ""). Without it, an unassigned issue left the assignee
	// attr empty and deny-assignee-outside-allowed's `attr.assignee != ""`
	// guard never fired -- update_issue/add_comment/transition_issue on any
	// unassigned ticket passed unchecked, regardless of ${jiraAllowedAssignees}.
	// deny-write-unassigned-issue (resource_jira.yaml) uses this marker to
	// deny that case specifically, without affecting create_issue (never
	// gated here at all -- deny-create-missing-assignee already covers it
	// via its own arg-based signal) or create_issue_link/link_to_epic
	// (outside jiraAssigneeGatedTools, so never set).
	if jiraAssigneeGatedTools[cp.Name] && res.ResourceType == jiraProjectResource {
		if assignee, _ := res.Attr["assignee"].(string); assignee == "" {
			if issueKey, _ := res.Attr["issueKey"].(string); issueKey != "" {
				resolved, derr := s.checkJiraIssueAssignee(ctx, issueKey)
				if derr != nil {
					log.Printf("deny: %s assignee lookup (issue=%q backend=%s): %v", cp.Name, issueKey, backend, derr)
					return deny(derr.Error()), nil
				}
				res.Attr["assigneeVerified"] = true
				if resolved != "" {
					res.Attr["assignee"] = resolved
				}
			}
		}
	}

	allowed, reason, err := s.decider.IsAllowed(ctx,
		s.principal.ID, s.principal.Roles,
		res.ResourceType, res.ID, res.Attr, res.Action)
	if err != nil {
		return deny(fmt.Sprintf("authorization check failed: %v", err)), nil
	}
	if !allowed {
		log.Printf("deny: %s on %s (tool=%s backend=%s reason=%q)", res.Action, res.ResourceType, cp.Name, backend, reason)
		// Surface the policy-authored reason (Cerbos rule `output`) when present
		// so the calling agent understands *why* and what to do instead (e.g.
		// "use REQUEST_CHANGES instead of APPROVE") rather than retrying blindly
		// or silently downgrading its own intent. Falls back to the generic
		// denyMessage when the matched rule has no output configured.
		msg := denyMessage
		if reason != "" {
			msg = reason
		}
		return deny(msg), nil
	}

	// Secret redaction: scrub credential-shaped strings out of the call's
	// arguments before it ever reaches vMCP -- see secrets_redact.go for why
	// this has to happen here and not in the egress-proxy. Runs on every
	// allowed call regardless of Force, since a tool with no force-override
	// can still carry a secret in one of its own arguments. Redaction never
	// denies (a pattern match on an otherwise-legitimate call shouldn't
	// break it) -- only rewrites via the same mutate() path Force already
	// uses.
	redactedArgs, redactedCount := redactArguments(cp.Arguments)
	if redactedCount > 0 {
		log.Printf("redact: %d secret-shaped value(s) scrubbed from %s args (backend=%s)", redactedCount, cp.Name, backend)
		cp.Arguments = redactedArgs
	}

	// Argument overrides for an allowed call: the tool's literal force values
	// plus any forceFrom expression evaluated against these args (GitLab's
	// draft-title rewrite is the latter — GitLab has no draft boolean, so the
	// override has to be derived from the incoming title). Evaluated against
	// the POST-redaction arguments so a forced value can't reintroduce a
	// scrubbed secret.
	overrides, err := s.engine.ForceOverrides(eval.CallInput{
		Tool: cp.Name, Backend: backend, Method: req.GetMethod(), Args: cp.Arguments,
	})
	if err != nil {
		return deny(fmt.Sprintf("force-override eval: %v", err)), nil
	}

	if len(overrides) == 0 && redactedCount == 0 {
		return pass(), nil
	}
	mutated, err := buildMutatedParams(cp, wrapped, overrides)
	if err != nil {
		// A shim-side malfunction (e.g. the tool's own args aren't marshalable) —
		// fail closed rather than forward an un-mutated, non-compliant call.
		return deny(fmt.Sprintf("force-override eval: %v", err)), nil
	}
	return mutate(mutated), nil
}

// buildMutatedParams applies literal force-overrides to cp.Arguments and
// re-serializes the tools/call params in the same shape the request arrived
// in (re-wrapped into call_tool{tool_name,parameters} if it came in that way).
func buildMutatedParams(cp callParams, wrapped bool, force map[string]any) ([]byte, error) {
	for k, v := range force {
		cp.Arguments[k] = v
	}
	if wrapped {
		return marshalNoHTMLEscape(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"tool_name": cp.Name, "parameters": cp.Arguments},
		})
	}
	return marshalNoHTMLEscape(map[string]any{"name": cp.Name, "arguments": cp.Arguments})
}

// checkModeration sends every free-text arg through s.moderationChecker.
// Fails OPEN on a moderation-service error (unlike every other gate in this
// file, which fails closed) -- a service outage shouldn't deny every write
// cluster-wide, and Cerbos authz still applies regardless.
func (s *Server) checkModeration(ctx context.Context, args map[string]any) error {
	strs := extractStrings(args)
	if len(strs) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, moderationTimeout)
	defer cancel()
	result, err := s.moderationChecker.Check(ctx, strs)
	if err != nil {
		log.Printf("moderation check failed, failing OPEN (see checkModeration doc comment): %v", err)
		return nil
	}
	if result.Flagged {
		return fmt.Errorf(
			"this call's content was flagged by the moderation gate (categories: %s); "+
				"rewrite the content and try again",
			strings.Join(result.FlaggedCategories, ", "),
		)
	}
	return nil
}

// extractStrings walks a decoded value and collects every string at any
// depth, unwrapping one level of JSON-encoded string (Jira's fields/
// additional_fields args are raw JSON strings).
func extractStrings(v any) []string {
	var out []string
	var walk func(any)
	walk = func(v any) {
		switch t := v.(type) {
		case string:
			if trimmed := strings.TrimSpace(t); len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
				var nested any
				if err := json.Unmarshal([]byte(t), &nested); err == nil {
					walk(nested)
					return
				}
			}
			out = append(out, t)
		case map[string]any:
			for _, val := range t {
				walk(val)
			}
		case []any:
			for _, val := range t {
				walk(val)
			}
		}
	}
	walk(v)
	return out
}

// checkNotionAncestry returns nil to allow the existing-page-write call
// through to Cerbos, or an error (used verbatim as the deny reason) to block
// it. Every failure path is fail-closed: an unconfigured gate, a missing
// page_id, a lookup error, and a confirmed not-under-any-allowed-parent all
// deny.
func (s *Server) checkNotionAncestry(ctx context.Context, pageID string) error {
	if s.notionAncestry == nil || len(s.notionAllowedParentIDs) == 0 {
		// The gate is mandatory for these tools: production always wires it
		// (main.go). Reaching here unconfigured means a broken deploy, not a
		// license to allow an unscoped page edit.
		return fmt.Errorf("notion ancestry gate not configured; denying write to page %q", pageID)
	}
	if pageID == "" {
		return fmt.Errorf("notion call has no page_id; cannot verify allowed-parent ancestry")
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	under, err := upstream.PageIsUnderAnyAncestor(ctx, s.notionAncestry, pageID, s.notionAllowedParentIDs)
	if err != nil {
		return fmt.Errorf("could not verify this Notion page is under an allowed parent folder (failing closed): %v", err)
	}
	if !under {
		return fmt.Errorf("this agent may only write to Notion pages under its allowed parent folders; page %q is not, so the write is denied", pageID)
	}
	return nil
}

// checkNotionPageAuthor reports whether pageID's real author does NOT match
// s.notionOperatorUserID -- a bool, not a direct deny, since this feeds
// res.Attr's pageAuthorMismatch key for Cerbos's own deny-not-own-page rule
// to evaluate (same inject-then-Cerbos-decides shape as
// checkGithubPRAuthor/checkJiraIssueAssignee below). Still fails closed (a
// non-nil error) on an unresolvable lookup, since an unverifiable author is
// not the same as a verified match.
func (s *Server) checkNotionPageAuthor(ctx context.Context, pageID string) (mismatch bool, err error) {
	if s.notionPageAuthor == nil || s.notionOperatorUserID == "" {
		// Mandatory for these tools: production always wires it (main.go) once
		// NOTION_USER_ID is configured. Reaching here unconfigured means a
		// broken/incomplete deploy, not a license to allow an unscoped page edit.
		return false, fmt.Errorf("notion page-author gate not configured; denying write to page %q", pageID)
	}
	if pageID == "" {
		return false, fmt.Errorf("notion call has no page_id; cannot verify page authorship")
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	authored, err := upstream.PageAuthoredByOperator(ctx, s.notionPageAuthor, pageID, s.notionOperatorUserID)
	if err != nil {
		return false, fmt.Errorf("could not verify this Notion page's author (failing closed): %v", err)
	}
	return !authored, nil
}

// checkLinearIssueTeam resolves issueID to its team AND current assignee via
// ONE live lookup, or returns an error (used verbatim as the deny reason) on
// any failure -- fail-closed contract mirrors checkNotionAncestry above: an
// unconfigured gate, a lookup error, or an issue with no resolvable team all
// deny rather than silently allow-through with no teamId attr (which would
// let the call skip Cerbos's team check entirely, the exact hole this gate
// closes). assignee may legitimately come back "" with no error -- a real
// Linear issue can have no assignee at all; only a genuinely unparseable
// assignee shape fails closed (see upstream.GetIssueDetails's contract).
func (s *Server) checkLinearIssueTeam(ctx context.Context, issueID string) (team, assignee string, err error) {
	if s.linearIssueTeam == nil {
		return "", "", fmt.Errorf("linear issue-team gate not configured; denying write for issue %q", issueID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	team, assignee, err = upstream.GetIssueDetails(ctx, s.linearIssueTeam, issueID)
	if err != nil {
		return "", "", fmt.Errorf("could not verify this Linear issue's team/assignee (failing closed): %v", err)
	}
	return team, assignee, nil
}

// checkLinearProjectTeam resolves projectID to its current team(s) via a
// live lookup, or returns an error (used verbatim as the deny reason) on any
// failure -- fail-closed contract mirrors checkLinearIssueTeam/
// checkNotionAncestry above: an unconfigured gate or a lookup error both
// deny rather than silently allow-through with no teams attr (which would
// let the call skip Cerbos's team check entirely, the exact hole this gate
// closes for save_project updates).
func (s *Server) checkLinearProjectTeam(ctx context.Context, projectID string) ([]string, error) {
	if s.linearProjectTeam == nil {
		return nil, fmt.Errorf("linear project-team gate not configured; denying update for project %q", projectID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	teams, err := upstream.ProjectTeams(ctx, s.linearProjectTeam, projectID)
	if err != nil {
		return nil, fmt.Errorf("could not verify this Linear project's team(s) (failing closed): %v", err)
	}
	return teams, nil
}

// checkGithubPRAuthor resolves owner/repo/pullNumber to the PR's real author
// login via a live lookup, or returns an error (used verbatim as the deny
// reason) on any failure -- fail-closed contract mirrors
// checkLinearIssueTeam/checkLinearProjectTeam above: an unconfigured gate or
// a lookup error both deny rather than silently allow-through with no
// prAuthor attr (which would let the call skip Cerbos's deny-not-own-pr rule
// entirely, the exact hole this gate closes).
func (s *Server) checkGithubPRAuthor(ctx context.Context, owner, repo string, pullNumber float64) (string, error) {
	if s.githubPRAuthor == nil {
		return "", fmt.Errorf("github PR-author gate not configured; denying write to %s/%s#%v", owner, repo, pullNumber)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	author, err := upstream.PRAuthor(ctx, s.githubPRAuthor, owner, repo, pullNumber)
	if err != nil {
		return "", fmt.Errorf("could not verify this GitHub PR's author (failing closed): %v", err)
	}
	return author, nil
}

// checkGitlabProjectCanonical resolves any accepted spelling of a GitLab
// project to its numeric id via a live lookup, or returns an error (used
// verbatim as the deny reason) on any failure -- fail-closed, same contract as
// checkGitlabMRAuthor above. An unresolvable project is not an allowlisted
// one, so a 404 denying is correct rather than a regression.
//
// A value that is ALREADY all-digits is returned as-is with no network call:
// GitLab project ids are numeric, a numeric project_id is by definition
// already canonical, and this keeps the common case (and every existing
// numeric-id allowlist entry) free of an extra round trip. A run of calls
// naming the project by path costs one lookup rather than one each, via the
// cache the client is wrapped in (internal/upstream/cache.go) rather than a
// gate-specific one here.
func (s *Server) checkGitlabProjectCanonical(ctx context.Context, projectID string) (string, error) {
	if isAllDigits(projectID) {
		return projectID, nil
	}
	if s.gitlabProjectCanonicalizer == nil {
		return "", fmt.Errorf("gitlab project-canonicalization gate not configured; denying call against project %q", projectID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	canonical, err := upstream.CanonicalProjectID(ctx, s.gitlabProjectCanonicalizer, projectID)
	if err != nil {
		return "", fmt.Errorf("could not resolve this GitLab project (failing closed): %v", err)
	}
	return canonical, nil
}

// isAllDigits reports whether s is a non-empty run of ASCII digits. Used to
// skip the canonicalization lookup for a project_id that is already a numeric
// id. Deliberately not strconv.Atoi: that accepts a leading sign and would
// treat "-148" as numeric.
func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// checkGitlabMRAuthor resolves project_id + merge_request_iid (or
// source_branch) to the MR's real author username via a live lookup, or returns
// an error (used verbatim as the deny reason) on any failure -- fail-closed
// contract mirrors checkGithubPRAuthor above: an unconfigured gate or a lookup
// error both deny rather than silently allow-through with no mrAuthor attr
// (which would let the call skip Cerbos's deny-not-own-mr rule entirely, the
// exact hole this gate closes).
func (s *Server) checkGitlabMRAuthor(ctx context.Context, projectID, mrIID, sourceBranch string) (string, error) {
	if s.gitlabMRAuthor == nil {
		return "", fmt.Errorf("gitlab MR-author gate not configured; denying write to project %s merge request %s%s", projectID, mrIID, sourceBranch)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	author, err := upstream.MRAuthor(ctx, s.gitlabMRAuthor, projectID, mrIID, sourceBranch)
	if err != nil {
		return "", fmt.Errorf("could not verify this GitLab merge request's author (failing closed): %v", err)
	}
	return author, nil
}

// checkAlertmanagerSilenceOwner resolves silenceID to its real createdBy via
// a live getSilences lookup, or returns an error (used verbatim as the deny
// reason) on any failure -- fail-closed contract mirrors checkGithubPRAuthor
// above: an unconfigured gate or a lookup error both deny rather than
// silently allow-through with no createdBy attr (which would let the call
// skip Cerbos's deny-not-own-silence rule entirely, the exact hole this gate
// closes).
func (s *Server) checkAlertmanagerSilenceOwner(ctx context.Context, getSilencesTool, silenceID string) (string, error) {
	if s.alertmanagerSilenceOwner == nil {
		return "", fmt.Errorf("alertmanager silence-owner gate not configured; denying delete of silence %q", silenceID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	createdBy, err := upstream.SilenceCreatedBy(ctx, getSilencesTool, s.alertmanagerSilenceOwner, silenceID)
	if err != nil {
		return "", fmt.Errorf("could not verify this Alertmanager silence's creator (failing closed): %v", err)
	}
	return createdBy, nil
}

// checkJiraIssueAssignee resolves issueKey to its CURRENT assignee via a
// live lookup, or returns an error (used verbatim as the deny reason) on any
// failure -- fail-closed contract mirrors checkLinearIssueTeam above: an
// unconfigured gate or a lookup error both deny rather than silently
// allow-through with no assignee attr (which would let the call skip
// Cerbos's deny-assignee-outside-allowed rule entirely, the exact hole this
// gate closes). A genuinely unassigned issue resolves to "" with no error
// (see upstream.IssueAssignee's contract) -- the caller only overwrites
// res.Attr["assignee"] when non-empty, leaving the has()-guarded Cerbos rule
// unaffected.
func (s *Server) checkJiraIssueAssignee(ctx context.Context, issueKey string) (string, error) {
	if s.jiraIssueAssignee == nil {
		return "", fmt.Errorf("jira issue-assignee gate not configured; denying write for issue %q", issueKey)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	assignee, err := upstream.IssueAssignee(ctx, s.jiraIssueAssignee, issueKey)
	if err != nil {
		return "", fmt.Errorf("could not verify this Jira issue's assignee (failing closed): %v", err)
	}
	return assignee, nil
}

// coerceNumber reads a JSON-decoded tool argument that should be a number.
// Arguments reach the shim as protobuf Struct values, so a well-formed number
// is a float64; a client that sends the same field as a JSON string or via a
// json.Number decoder must still be understood, because the alternative is a
// gate that silently skips itself on a shape it didn't expect.
func coerceNumber(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case float32:
		return float64(n), true
	case int:
		return float64(n), true
	case int32:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(n), 64)
		return f, err == nil
	}
	return 0, false
}

// scalarArg reads args[key] and renders a string or JSON number as its string
// form ("" for a missing key or any other type). GitLab's project_id and
// merge_request_iid are declared strings by the tool schema, but a caller naming
// a project or MR by its number sends a JSON number (float64 after decoding), so
// a bare args[key].(string) assertion reads it as absent -- the same silent-miss
// the gitlabProjectAttr CEL helper guards against on the policy-attr side.
// Numbers route through coerceNumber so this shares one definition of "what
// shapes count as a number" with the PR-number gate rather than drifting from it.
func scalarArg(args map[string]any, key string) string {
	v, ok := args[key]
	if !ok {
		return ""
	}
	if s, isStr := v.(string); isStr {
		return s
	}
	if n, isNum := coerceNumber(v); isNum {
		return strconv.FormatFloat(n, 'f', -1, 64)
	}
	return ""
}

// pagerdutyIncidentIDsFromArgs extracts every incident id a
// manage_incidents/add_note_to_incident call targets directly from the raw
// tool arguments (not res.Attr/res.ID, since manage_incidents' id/attr shape
// is a fixed literal "'manage_incidents'" per mapping.yaml, not the actual
// incident_ids -- see resource_pagerduty.yaml's own comment on why no
// bulk-size cap exists there). manage_incidents carries a nested
// manage_request.incident_ids array; add_note_to_incident carries a single
// top-level incident_id string. Non-string array elements are skipped
// (better to check what's checkable than fail the whole call over one
// malformed entry, same posture as lookupCIStringSlice elsewhere in this
// shim).
func pagerdutyIncidentIDsFromArgs(toolName string, args map[string]any) []string {
	if _, ok := pagerdutyManageIncidentsTools[toolName]; ok {
		req, _ := args["manage_request"].(map[string]any)
		ids, _ := req["incident_ids"].([]any)
		out := make([]string, 0, len(ids))
		for _, id := range ids {
			if s, ok := id.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	}
	if _, ok := pagerdutyAddNoteTools[toolName]; ok {
		if id, ok := args["incident_id"].(string); ok && id != "" {
			return []string{id}
		}
	}
	return nil
}

// checkPagerdutyIncidentServices resolves every incidentID to its owning
// service id via a live lookup per id, or returns an error (used verbatim
// as the deny reason) on ANY single failure -- fail-closed contract mirrors
// checkLinearIssueTeam/checkLinearProjectTeam above: an unconfigured gate,
// or any one incident's lookup failing, denies the WHOLE call rather than
// silently checking only the incidents that happened to resolve (a
// partially-resolved batch is not a safe signal to check against an
// allowlist -- see resource_pagerduty.yaml's own no-bulk-cap rationale for
// why a batch call must be treated as a single unit here, not per-incident).
func (s *Server) checkPagerdutyIncidentServices(ctx context.Context, getIncidentTool string, incidentIDs []string) ([]string, error) {
	if s.pagerdutyIncidentService == nil {
		return nil, fmt.Errorf("pagerduty incident-service gate not configured; denying write for incidents %v", incidentIDs)
	}
	serviceIDs := make([]string, 0, len(incidentIDs))
	for _, id := range incidentIDs {
		lookupCtx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
		serviceID, err := upstream.IncidentServiceID(lookupCtx, getIncidentTool, s.pagerdutyIncidentService, id)
		cancel()
		if err != nil {
			return nil, fmt.Errorf("could not verify PagerDuty incident %q's owning service (failing closed): %v", id, err)
		}
		serviceIDs = append(serviceIDs, serviceID)
	}
	return serviceIDs, nil
}

// resolveBackend enforces exactly-one mapped backend in service_names.
func (s *Server) resolveBackend(names []string) (string, error) {
	if len(names) != 1 {
		return "", fmt.Errorf("expected exactly one service name, got %d", len(names))
	}
	name := names[0]
	if _, ok := s.mapping.Backends[name]; !ok {
		return "", fmt.Errorf("backend %q not mapped", name)
	}
	return name, nil
}

// pass returns a clean allow with NO side-effect channels set.
func pass() *pb.McpRequestResult {
	return &pb.McpRequestResult{Result: &pb.McpRequestResult_Pass{Pass: &pb.Pass{}}}
}

// isInternalBackend reports whether service_names names the vmcp-internal MCP
// target. It is the network/route-level lock's app-layer companion.
func isInternalBackend(names []string) bool {
	for _, n := range names {
		if n == internalBackendName {
			return true
		}
	}
	return false
}

// isSelfRequest reports whether req carries the shim's secret self-token in the
// upstream.SelfHeaderName header. Constant-time compare; a missing token config
// always returns false, which makes the internal backend fail closed. Header
// keys are matched case-insensitively.
func (s *Server) isSelfRequest(req *pb.McpRequest) bool {
	if s.selfToken == "" {
		return false
	}
	want := []byte(s.selfToken)
	for _, h := range req.GetHeaders() {
		if strings.EqualFold(h.GetKey(), upstream.SelfHeaderName) {
			if subtle.ConstantTimeCompare(h.GetValue(), want) == 1 {
				return true
			}
		}
	}
	return false
}

// deny returns a PERMISSION_DENIED AuthorizationError with NO side-effect channels.
func deny(reason string) *pb.McpRequestResult {
	return &pb.McpRequestResult{
		Result: &pb.McpRequestResult_Error{
			Error: &pb.AuthorizationError{
				Code:   pb.AuthorizationError_PERMISSION_DENIED,
				Reason: reason,
			},
		},
	}
}

// mutate replaces the JSON-RPC params before the gateway forwards the call
// upstream. Only reached after Cerbos has already allowed the (unmutated)
// call, so the resource checked and the resource forwarded always agree on
// owner/repo/branch — only literal force-override keys (e.g. draft) change.
func mutate(params []byte) *pb.McpRequestResult {
	return &pb.McpRequestResult{Result: &pb.McpRequestResult_Mutated{Mutated: params}}
}

// responsePass returns a clean allow with no mutation, for CheckResponse.
func responsePass() *pb.McpResponseResult {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Pass{Pass: &pb.Pass{}}}
}

// responseMutate replaces the JSON-RPC result before it reaches the model,
// mirroring mutate()'s request-side contract: must parse as a valid result
// for the method, or the gateway treats it as a protocol violation.
func responseMutate(result []byte) *pb.McpResponseResult {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Mutated{Mutated: result}}
}

// responseDeny withholds a tool's RESULT entirely -- the CheckResponse-side
// equivalent of deny() on the request side. McpResponseResult's oneof
// carries the same AuthorizationError shape CheckRequest's deny() uses
// (pb.McpResponseResult_Error{Error: *pb.AuthorizationError}), so this is a
// first-class denial, not a downgraded pass/mutate. Used only by
// checkPromptInjection's confirmed-detection path (HAH-107) -- deny only,
// never a partial mutation/strip, same "no safe partial fix" posture
// checkModeration already uses.
func responseDeny(reason string) *pb.McpResponseResult {
	return &pb.McpResponseResult{
		Result: &pb.McpResponseResult_Error{
			Error: &pb.AuthorizationError{
				Code:   pb.AuthorizationError_PERMISSION_DENIED,
				Reason: reason,
			},
		},
	}
}

// promptInjectionJudgeTimeout bounds a single stage-2 judge call -- sibling
// to moderationTimeout, same fail-open-on-timeout posture (see
// checkPromptInjection).
const promptInjectionJudgeTimeout = 10 * time.Second

// maxJudgeCallsPerResponse caps the TOTAL number of stage-2 judge calls
// checkPromptInjection will make for a single CheckResponse invocation,
// across every string value and every matched pattern in the decoded body.
// Without this, a single hostile response containing many distinct
// stage-1-matching phrases (or many strings each matching a different
// pattern) could fan out an unbounded number of sequential LLM calls --
// real cost and latency an attacker fully controls (the content triggering
// each call comes straight from the externally-sourced tool result), and
// enough latency to risk exhausting the AgentgatewayPolicy guardrail RPC's
// own deadline. Hitting the cap before a confirmed detection is treated as
// a DENY, not a pass-through: a response cheap enough to synthesize this
// many candidate matches is itself a strong signal, and letting the
// remaining unchecked matches through unverified would reopen exactly the
// bypass stage 2 exists to close.
const maxJudgeCallsPerResponse = 20

// CheckResponse scrubs credential-shaped strings out of a tool's RESULT
// before it reaches the model -- the response-side half of the redaction
// gap secrets_redact.go documents. Only tools/call responses carry
// meaningful content to scrub (other methods, and the empty/unparseable
// case, pass through unmutated). Redaction failures never deny -- a
// response that can't be parsed/re-encoded passes through as-is rather
// than breaking an otherwise-successful tool call; this is a
// best-effort, defense-in-depth layer, not a hard boundary (see
// secrets_redact.go's doc comment on why deny is never the right response
// here).
//
// checkPromptInjection (HAH-107) runs alongside redaction on the SAME raw
// response bytes, for the SAME redactableResponseMethods set. Unlike
// redaction, a confirmed prompt-injection detection DENIES the call --
// checked and returned BEFORE the redaction pass runs, since there's no
// point redacting a result that's about to be withheld entirely.
func (s *Server) CheckResponse(ctx context.Context, resp *pb.McpResponse) (*pb.McpResponseResult, error) {
	// The internal policy runs ordinary methods at Request only. Agentgateway
	// v1.4.1 cannot run request-phase guardrails for resources/subscribe,
	// resources/unsubscribe, or completion/complete, so it sends those methods
	// here at Response instead. The shim never needs them: deny unconditionally
	// so a tokenless caller cannot use a response-only method to bypass the
	// app-layer lock. This also fails closed if the policy is ever accidentally
	// changed to run any other internal response phase.
	if isInternalBackend(resp.GetServiceNames()) {
		log.Printf("deny: response-phase method not permitted on the reserved vmcp-internal backend (method=%q backend=%v)", resp.GetMethod(), resp.GetServiceNames())
		return responseDeny(fmt.Sprintf("method %q is not permitted on the vmcp-internal backend", resp.GetMethod())), nil
	}

	if !redactableResponseMethods[resp.GetMethod()] {
		return responsePass(), nil
	}
	raw := resp.GetMcpResponse()
	if len(raw) == 0 {
		return responsePass(), nil
	}
	if reason, blocked := s.checkPromptInjection(ctx, raw, resp.GetServiceNames()); blocked {
		return responseDeny(reason), nil
	}
	redacted, n := redactRawJSON(raw)
	if n == 0 {
		return responsePass(), nil
	}
	log.Printf("redact: %d secret-shaped value(s) scrubbed from an MCP response (method=%s backend=%v)", n, resp.GetMethod(), resp.GetServiceNames())
	return responseMutate(redacted), nil
}

// checkPromptInjection runs the two-stage prompt-injection gate (HAH-107)
// over redactable MCP responses. A no-op (false, "") when the gate is disabled
// (nil detector, the per-cluster PROMPT_INJECTION_DETECTION toggle in
// main.go).
//
// Stage 1 (s.promptInjectionDetector, internal/promptinjection's regex
// registry) is deliberately broad/high-recall and expected to over-match
// benign text -- it exists only to decide whether stage 2 runs at all,
// which is why a stage-1-only match never blocks by itself.
//
// Stage 2 (s.promptInjectionJudge) runs ONLY on text stage 1 already
// flagged -- the cost-control mechanism, since most reads never trigger it.
// It sends the matched pattern name plus a bounded text window (see
// promptinjection.WindowAround) to a small LLM judge and asks a strict
// yes/no. A confirmed ("yes") verdict DENIES the call -- deny only, never a
// partial mutation/strip, mirroring checkModeration's "no safe partial fix"
// posture. This is a deliberate, reviewed upgrade from the ticket's own
// suggested log-only rollout: the two-stage design's judge confirmation
// step is what makes blocking safe, by filtering stage 1's noisy matches
// down to a confirmed detection before anything is denied.
//
// A stage-2 SERVICE error (timeout, non-200, network error -- anything
// Judge.Confirm returns as a non-nil error) fails OPEN: the call passes
// through, logged clearly as "judge unavailable... NOT enforced" -- an
// unrelated OpenAI outage should not deny every matching read cluster-wide.
// This is distinct from an unconfirmed ("no", or a clearly-parsed-but-
// ambiguous) judge verdict, which is a successful call that simply doesn't
// confirm a detection -- that case also passes through, but is not a
// fail-open case (see promptinjection.Client.Confirm's doc comment for the
// service-error vs. ambiguous-verdict distinction).
//
// Every stage-1 match is logged regardless of stage-2 outcome (pattern
// name, backend, and judge verdict/error), so there is still a debuggable
// trail even though this gate now enforces.
//
// Scope: McpResponse carries service_names (the single mapped backend name,
// e.g. "vmcp" for everything behind this shim's one vMCP target) and the raw
// JSON-RPC result bytes -- it carries neither the original tool name nor a
// per-backend (firecrawl/tavily/notion/...) breakdown, since every call is
// muxed through one AgentgatewayBackend target. There is therefore no
// signal here to scope detection to only the read-shaped tools the ticket
// names (firecrawl_scrape, tavily_extract, notion-fetch, jira_get_issue,
// github pull_request_read, gitlab get_merge_request_diffs, ...). This scans
// every redactable response when enabled, matching the broad-by-default posture WithModeration
// takes for unmapped backends, but now with a real cost (stage 2 is not
// free) -- that's acceptable because stage 2 only runs on stage-1 matches,
// which are rare in ordinary traffic, and maxJudgeCallsPerResponse bounds
// the worst case regardless.
//
// promptinjection.Detect reports EVERY occurrence of every matched pattern
// (up to its own per-pattern cap), not just the first -- judging only a
// string's first occurrence of a pattern would let a real injection later
// in the same document hide behind an earlier, judged-benign occurrence of
// that same pattern (e.g. a sentence describing the attack shape, followed
// later by the actual attack). maxJudgeCallsPerResponse then bounds the
// TOTAL judge calls this can fan out to across the whole response.
func (s *Server) checkPromptInjection(ctx context.Context, raw []byte, serviceNames []string) (reason string, blocked bool) {
	if s.promptInjectionDetector == nil {
		return "", false
	}
	calls := 0
	for _, str := range extractResponseStrings(raw) {
		res := s.promptInjectionDetector.Detect(str)
		if !res.Matched {
			continue
		}
		for i, name := range res.MatchedNames {
			if calls >= maxJudgeCallsPerResponse {
				// The response-wide judge-call budget is exhausted with
				// stage-1 matches still unverified. Deny rather than
				// silently pass the remainder through unchecked -- a
				// response cheap enough to synthesize this many candidate
				// matches is itself suspicious, and passing unverified
				// matches through would reopen the exact fan-out bypass
				// this cap exists to close (see maxJudgeCallsPerResponse's
				// doc comment).
				log.Printf("prompt-injection: judge-call budget (%d) exhausted with unverified matches remaining, denying call (backend=%v)", maxJudgeCallsPerResponse, serviceNames)
				return fmt.Sprintf("response content matched %d+ prompt-injection candidates, exceeding the per-response verification budget; tool result withheld", maxJudgeCallsPerResponse), true
			}
			offset := 0
			if i < len(res.MatchedOffsets) {
				offset = res.MatchedOffsets[i]
			}
			calls++
			verdict, err := s.confirmPromptInjection(ctx, name, str, offset)
			if err != nil {
				log.Printf("prompt-injection: judge unavailable, matched pattern %s NOT enforced (fail-open): %v (backend=%v)", name, err, serviceNames)
				continue
			}
			if verdict {
				log.Printf("prompt-injection: pattern %s CONFIRMED by judge, denying call (backend=%v)", name, serviceNames)
				return fmt.Sprintf("response content flagged by prompt-injection detector (pattern: %s); tool result withheld", name), true
			}
			log.Printf("prompt-injection: pattern %s matched stage 1 but judge did not confirm (backend=%v)", name, serviceNames)
		}
	}
	return "", false
}

// confirmPromptInjection runs stage 2 for a single stage-1 match. A nil
// promptInjectionJudge (gate enabled via detector but no judge configured)
// is treated the same as a judge-service error -- fail open, since blocking
// on an unconfirmed regex match alone is exactly the noisy behavior stage 2
// exists to prevent (see checkPromptInjection's doc comment).
func (s *Server) confirmPromptInjection(ctx context.Context, patternName, text string, offset int) (bool, error) {
	if s.promptInjectionJudge == nil {
		return false, fmt.Errorf("no prompt-injection judge configured")
	}
	ctx, cancel := context.WithTimeout(ctx, promptInjectionJudgeTimeout)
	defer cancel()
	window := promptinjection.WindowAround(text, offset)
	return s.promptInjectionJudge.Confirm(ctx, patternName, window)
}

// extractResponseStrings walks a decoded JSON-RPC result body and returns
// every string value found, depth-first -- reuses the exact same decode as
// redactRawJSON/redactValue would, but a pure read (no rewrite), since
// checkPromptInjection never mutates. An unparseable body yields no strings
// (fail-open, same posture as redaction's own unparseable-body path).
func extractResponseStrings(raw []byte) []string {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return nil
	}
	var out []string
	collectStrings(decoded, &out)
	return out
}

func collectStrings(v any, out *[]string) {
	switch t := v.(type) {
	case string:
		*out = append(*out, t)
	case map[string]any:
		for _, val := range t {
			collectStrings(val, out)
		}
	case []any:
		for _, val := range t {
			collectStrings(val, out)
		}
	}
}
