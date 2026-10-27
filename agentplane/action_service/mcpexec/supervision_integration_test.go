package mcpexec

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestTheGroupOffersTheServersToolsAsItsActions(t *testing.T) {
	server := newServer()
	server.AddTool(&mcp.Tool{
		Name: "test_annotated", Title: "Test title", Description: "test description", InputSchema: textSchema,
		Annotations: &mcp.ToolAnnotations{
			Title: "Test annotation title", ReadOnlyHint: true, DestructiveHint: new(false), IdempotentHint: true,
			OpenWorldHint: new(true),
		},
	}, answering(&calls{}, textResult("unused")))
	server.AddTool(&mcp.Tool{Name: "test_bare", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	// A name the catalog cannot key is left out, not the whole list.
	server.AddTool(&mcp.Tool{Name: "Test.Unkeyed", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	groups := mcpGroup(t, httpConfig(newUpstream(t, server, nil).url()))
	before := time.Now()
	supervise(t, groups, nil, executorTiming())

	g := awaitGroup(t, groups, offering("test_annotated", "test_bare"))
	annotated, bare := g.Actions["test_annotated"], g.Actions["test_bare"]
	if annotated.Description != "test description" || *annotated.Title != "Test title" {
		t.Errorf("test_annotated is %+v", annotated)
	}
	sameJSON(t, annotated.InputSchema, textSchema)
	if a := annotated.Annotations; a == nil || *a.Title != "Test annotation title" || !*a.ReadOnlyHint ||
		*a.DestructiveHint || !*a.IdempotentHint || !*a.OpenWorldHint {
		t.Errorf("test_annotated's annotations are %+v", annotated.Annotations)
	}
	if bare.Description != "MCP tool test_bare" || bare.Title != nil || bare.Annotations != nil {
		t.Errorf("test_bare is %+v", bare)
	}
	h := g.Health
	if h.State != action_servicepb.McpLifecycle_MCP_LIFECYCLE_AVAILABLE || h.Reason != nil || h.Detail != nil ||
		h.RetryAt != nil || h.LastDiscoveryAt.AsTime().Before(before) || h.Failures != 0 {
		t.Errorf("health %v", h)
	}
}

func TestTheGroupFollowsTheToolsTheServerSaysChanged(t *testing.T) {
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_first", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	groups := mcpGroup(t, httpConfig(newUpstream(t, server, nil).url()))
	// The tools are read again every five minutes, so only the server's notification can change them within the test.
	supervise(t, groups, nil, executorTiming())
	awaitGroup(t, groups, offering("test_first"))

	server.AddTool(&mcp.Tool{Name: "test_second", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	awaitGroup(t, groups, offering("test_first", "test_second"))
	server.RemoveTools("test_first")
	awaitGroup(t, groups, offering("test_second"))
	server.AddTool(&mcp.Tool{Name: "test_second", Description: "test changed", InputSchema: objectSchema},
		answering(&calls{}, textResult("unused")))
	awaitGroup(t, groups, func(g *catalog.Group) bool {
		return offering("test_second")(g) && g.Actions["test_second"].Description == "test changed"
	})
}

func TestTheGroupReadsTheToolsAgainEveryRefresh(t *testing.T) {
	first := newServer()
	first.AddTool(&mcp.Tool{Name: "test_first", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	// A stateless server sends no notification: only the refresh can see its tools change.
	u := newUpstream(t, first, &mcp.StreamableHTTPOptions{Stateless: true})
	groups := mcpGroup(t, httpConfig(u.url()))
	timing := executorTiming()
	timing.Refresh = 50 * time.Millisecond
	supervise(t, groups, nil, timing)
	awaitGroup(t, groups, offering("test_first"))

	second := newServer()
	second.AddTool(&mcp.Tool{Name: "test_second", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	u.server.Store(second)
	awaitGroup(t, groups, offering("test_second"))
}

func TestAToolListTheCatalogCannotHoldWithdrawsTheGroupUntilItIsFixed(t *testing.T) {
	invalid := newServer()
	invalid.AddTool(&mcp.Tool{
		Name: "test_invalid", InputSchema: json.RawMessage(`{"type": "object", "properties": {"test": {"type": 1}}}`),
	}, answering(&calls{}, textResult("unused")))
	u := newUpstream(t, invalid, &mcp.StreamableHTTPOptions{Stateless: true})
	groups := mcpGroup(t, httpConfig(u.url()))
	supervise(t, groups, nil, executorTiming())
	g := awaitGroup(t, groups, unavailableFor(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_INVALID_CATALOG,
		func(detail string) bool { return strings.Contains(detail, "test_invalid") }))
	if len(g.Actions) != 0 || g.Health.RetryAt == nil || g.Health.Failures == 0 {
		t.Errorf("the withdrawn group offers %v with health %v", g.Actions, g.Health)
	}

	fixed := newServer()
	fixed.AddTool(&mcp.Tool{Name: "test_fixed", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	u.server.Store(fixed)
	awaitGroup(t, groups, offering("test_fixed"))
}

func TestAServerThatIsDownLeavesItsGroupUnavailableUntilItIsBack(t *testing.T) {
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_tool", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	u := newUpstream(t, server, nil)
	u.down.Store(true)
	groups := mcpGroup(t, httpConfig(u.url()))
	supervise(t, groups, nil, executorTiming())
	g := awaitGroup(t, groups, unavailableFor(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_CONNECT_FAILED, anyDetail))
	if g.Health.State != action_servicepb.McpLifecycle_MCP_LIFECYCLE_DISCONNECTED || g.Health.Failures == 0 ||
		g.Health.RetryAt == nil {
		t.Errorf("health %v", g.Health)
	}

	u.down.Store(false)
	g = awaitGroup(t, groups, offering("test_tool"))
	// The failures stay counted until the group has stayed offered for Timing.Stable, so a server that fails again
	// right after it came back keeps backing off.
	if g.Health.Failures == 0 || g.Health.RetryAt != nil {
		t.Errorf("health %v", g.Health)
	}
}

func TestConfiguredHeadersAndAMountedBearerAreSentOnEveryRequest(t *testing.T) {
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_tool", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	u := newUpstream(t, server, nil)
	bearer := filepath.Join(t.TempDir(), "test-bearer")
	groups := mcpGroup(t, `{"transport": "streamable-http", "url": "`+u.url()+`", "auth": "static_bearer",
		"bearer_file": "`+bearer+`", "headers": {"X-Test-Header": "test-value"}}`)
	supervise(t, groups, nil, executorTiming())
	connectFailed := action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_CONNECT_FAILED
	// The bearer is read for each session, so one mounted after the service started is used.
	awaitGroup(t, groups, unavailableFor(connectFailed, func(detail string) bool { return strings.Contains(detail, "unavailable") }))
	if err := os.WriteFile(bearer, []byte("\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	awaitGroup(t, groups, unavailableFor(connectFailed, func(detail string) bool { return strings.Contains(detail, "empty") }))
	if err := os.WriteFile(bearer, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	awaitGroup(t, groups, offering("test_tool"))

	received := u.received()
	if len(received) == 0 {
		t.Fatal("no request reached the server")
	}
	for _, headers := range received {
		if headers.Get("Authorization") != "Bearer test-token" || headers.Get("X-Test-Header") != "test-value" {
			t.Errorf("a request carried %v", headers)
		}
	}
}

// link records serverID's linkage holding token, as the linkage flow does: expiresAt nil gives a token without an
// expiry, and refreshFailure the action a failed refresh asks for.
func (f fixture) link(t *testing.T, serverID, token string, expiresAt *time.Time, refreshFailure *string) {
	t.Helper()
	var failureError *string
	if refreshFailure != nil {
		failureError = new("test refresh failure")
	}
	var state uuid.UUID
	if err := f.db.QueryRowContext(t.Context(), `
		INSERT INTO mcp_oauth_token_state (id, server_id, access_token, token_type, scope, expires_at, token_revision,
			updated_at, refresh_failure_count, refresh_failure_action, refresh_failure_error)
		VALUES ($1, $2, $3, 'Bearer', '[]', $4, 1, now(), 0, $5, $6)
		ON CONFLICT (server_id) DO UPDATE SET access_token = excluded.access_token, expires_at = excluded.expires_at,
			refresh_failure_action = excluded.refresh_failure_action,
			refresh_failure_error = excluded.refresh_failure_error
		RETURNING id`,
		uuid.New(), serverID, token, expiresAt, refreshFailure, failureError).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.ExecContext(t.Context(), `
		INSERT INTO mcp_server_linkage (server_id, server_url, revision, scopes, token_state_id)
		VALUES ($1, 'https://mcp.test.example/mcp', 1, '[]', $2)
		ON CONFLICT (server_id) DO UPDATE SET token_state_id = excluded.token_state_id`, serverID, state); err != nil {
		t.Fatal(err)
	}
}

// linkages are the OAuth linkages of the database, where testGroup is the one server configured.
func (f fixture) linkages(t *testing.T) *linkage.Authority {
	t.Helper()
	authority, err := linkage.New(f.db, map[string]linkage.Server{testGroup: {
		ServerID: testGroup, ServerURL: "https://mcp.test.example/mcp", ClientID: new("test-client"),
		RedirectURI: "https://actions.test.example/callback",
	}}, linkage.ClientMetadata{})
	if err != nil {
		t.Fatal(err)
	}
	return authority
}

func oauthConfig(url string) string {
	return `{"transport": "streamable-http", "url": "` + url + `", "auth": "oauth", "server_id": "` + testGroup + `"}`
}

var linkageUnavailable = action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_LINKAGE_UNAVAILABLE

func TestAnOAuthGroupWaitsForItsLinkageAndSendsItsCurrentToken(t *testing.T) {
	f := newFixture(t)
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_tool", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	u := newUpstream(t, server, nil)
	groups := mcpGroup(t, oauthConfig(u.url()))
	supervise(t, groups, f.linkages(t), executorTiming())
	awaitGroup(t, groups, unavailableFor(linkageUnavailable, func(detail string) bool { return detail == "no account is linked" }))

	expired := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	f.link(t, testGroup, "test-expired-token", &expired, nil)
	awaitGroup(t, groups, unavailableFor(linkageUnavailable, func(detail string) bool {
		return detail == "the access token expired at 2026-01-02 03:04:05 UTC and has not been refreshed"
	}))
	f.link(t, testGroup, "test-degraded-token", nil, new("reconnect"))
	awaitGroup(t, groups, unavailableFor(linkageUnavailable, func(detail string) bool {
		return strings.Contains(detail, "link the account again")
	}))
	// A refresh that is still being retried leaves the token usable.
	f.link(t, testGroup, "test-first-token", nil, new("retrying"))
	awaitGroup(t, groups, offering("test_tool"))
	if len(u.received()) == 0 {
		t.Fatal("no request reached the server")
	}
	for _, headers := range u.received() {
		if headers.Get("Authorization") != "Bearer test-first-token" {
			t.Errorf("a request carried %q", headers.Get("Authorization"))
		}
	}

	// Each request carries the token the linkage holds when it is sent: the next read of the tools, on the same
	// session, carries the refreshed one.
	f.link(t, testGroup, "test-second-token", nil, nil)
	server.AddTool(&mcp.Tool{Name: "test_second", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	awaitGroup(t, groups, offering("test_tool", "test_second"))
	if !slices.ContainsFunc(u.received(), func(headers http.Header) bool {
		return headers.Get("Authorization") == "Bearer test-second-token"
	}) {
		t.Error("no request carried the refreshed token")
	}
}

// An offered group reads its tools again only every five minutes, so only the change tells it its account is lost.
func TestAGroupReadsItsLinkageAgainWhenToldItChanged(t *testing.T) {
	f := newFixture(t)
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_tool", InputSchema: objectSchema}, answering(&calls{}, textResult("unused")))
	groups := mcpGroup(t, oauthConfig(newUpstream(t, server, nil).url()))
	f.link(t, testGroup, "test-token", nil, nil)
	s := supervise(t, groups, f.linkages(t), executorTiming())
	awaitGroup(t, groups, offering("test_tool"))
	f.link(t, testGroup, "test-token", nil, new("reconnect"))
	s.LinkageChanged()
	awaitGroup(t, groups, unavailableFor(linkageUnavailable, func(detail string) bool {
		return strings.Contains(detail, "link the account again")
	}))
}

func TestAStdioServerRunsWithoutThisProcesssEnvironment(t *testing.T) {
	f := newFixture(t)
	t.Setenv("MCPEXEC_TEST_SECRET", "test-secret")
	command, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	config, err := json.Marshal(map[string]any{
		"transport": "stdio", "command": command, "env": map[string]string{stdioServerVariable: "1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	groups := mcpGroup(t, string(config))
	s := supervise(t, groups, nil, executorTiming())
	awaitGroup(t, groups, offering("test_environment"))
	f.work(t, groups, s.Executor, workerTiming())

	request := f.await(t, f.allowed(t, "test_environment", `{}`), action.StateSucceeded)
	var result struct {
		StructuredContent struct{ Names []string } `json:"structuredContent"`
	}
	if err := json.Unmarshal(request.Execution.Result, &result); err != nil {
		t.Fatal(err)
	}
	allowed := append(slices.Clone(defaultEnvironment), stdioServerVariable)
	for _, name := range result.StructuredContent.Names {
		if !slices.Contains(allowed, name) {
			t.Errorf("the server's environment holds %s; it may hold %v", name, allowed)
		}
	}
	if !slices.Contains(result.StructuredContent.Names, stdioServerVariable) {
		t.Errorf("the configured variable is missing from %v", result.StructuredContent.Names)
	}
}
