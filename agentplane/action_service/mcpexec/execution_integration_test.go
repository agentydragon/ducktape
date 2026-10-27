package mcpexec

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// offered is a group of server's tools, offered, over a fixture with a worker running its Executions.
func offered(t *testing.T, server *mcp.Server, options *mcp.StreamableHTTPOptions, timing Timing, names ...string) (fixture, *upstream, supervised) {
	t.Helper()
	f := newFixture(t)
	u := newUpstream(t, server, options)
	groups := mcpGroup(t, httpConfig(u.url()))
	s := supervise(t, groups, nil, timing)
	awaitGroup(t, groups, offering(names...))
	f.work(t, groups, s.Executor, workerTiming())
	return f, u, s
}

func TestAnAllowedExecutionCallsTheToolOnceAndKeepsItsWholeAnswer(t *testing.T) {
	echo := &calls{}
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: textSchema}, answering(echo, &mcp.CallToolResult{
		Meta: mcp.Meta{"test": "test-meta"},
		Content: []mcp.Content{
			&mcp.TextContent{Text: "test text"}, &mcp.ImageContent{Data: []byte("test"), MIMEType: "image/png"},
		},
		StructuredContent: map[string]any{"echoed": "test text"},
	}))
	f, _, _ := offered(t, server, nil, executorTiming(), "test_echo")

	request := f.await(t, f.allowed(t, "test_echo", `{"text": "test text"}`), action.StateSucceeded)
	sameJSON(t, request.Execution.Result, json.RawMessage(`{
		"_meta": {"test": "test-meta"},
		"content": [{"type": "text", "text": "test text"}, {"type": "image", "data": "dGVzdA==", "mimeType": "image/png"}],
		"structuredContent": {"echoed": "test text"}
	}`))
	made := echo.made()
	if len(made) != 1 {
		t.Fatalf("the tool was called %d times", len(made))
	}
	sameJSON(t, made[0], json.RawMessage(`{"text": "test text"}`))
}

func TestAToolsErrorResultIsItsAnswerUnlessItCannotTellTheOutcome(t *testing.T) {
	for _, test := range []struct {
		name   string
		answer *mcp.CallToolResult
		state  action.State
	}{
		{
			"an error result", &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: "test failure"}}},
			action.StateSucceeded,
		},
		{"an unknown outcome", &mcp.CallToolResult{IsError: true, Content: []mcp.Content{
			&mcp.TextContent{Text: `{"kind": "execution_unknown", "message": "test"}`},
		}}, action.StateExecutionUnknown},
	} {
		t.Run(test.name, func(t *testing.T) {
			tool := &calls{}
			server := newServer()
			server.AddTool(&mcp.Tool{Name: "test_tool", InputSchema: objectSchema}, answering(tool, test.answer))
			f, _, _ := offered(t, server, nil, executorTiming(), "test_tool")
			request := f.await(t, f.allowed(t, "test_tool", `{}`), test.state)
			if test.state == action.StateSucceeded {
				sameJSON(t, request.Execution.Result, json.RawMessage(`{"content": [{"type": "text", "text": "test failure"}],
					"isError": true}`))
			} else if kind := errorKind(t, request); kind != string(store.UnknownAdapterOutcome) {
				t.Errorf("kind %q", kind)
			}
			if made := tool.made(); len(made) != 1 {
				t.Errorf("the tool was called %d times", len(made))
			}
		})
	}
}

func TestAnExecutionReadsTheToolsAgainBeforeItCallsOne(t *testing.T) {
	for _, test := range []struct {
		name string
		// now is the tool the server offers when the Execution starts; nil when it offers none.
		now  *mcp.Tool
		kind string
	}{
		{"of a tool the server no longer offers", nil, "unknown_action"},
		{"whose schema the arguments no longer match", &mcp.Tool{Name: "test_echo", InputSchema: json.RawMessage(
			`{"type": "object", "properties": {"text": {"type": "integer"}}}`,
		)}, "incompatible_action_schema"},
		{"whose schema is no longer a JSON Schema", &mcp.Tool{Name: "test_echo", InputSchema: json.RawMessage(
			`{"type": "object", "properties": {"text": {"type": 1}}}`,
		)}, "mcp_invalid_schema"},
	} {
		t.Run(test.name, func(t *testing.T) {
			before, after := &calls{}, &calls{}
			first := newServer()
			first.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: textSchema}, answering(before, textResult("test text")))
			// A stateless server sends no notification, and the tools are read again every five minutes: the
			// group offers the first server's tool while the Execution reads the second's.
			f, u, s := offered(t, first, &mcp.StreamableHTTPOptions{Stateless: true}, executorTiming(), "test_echo")
			second := newServer()
			if test.now != nil {
				second.AddTool(test.now, answering(after, textResult("test text")))
			}
			u.server.Store(second)

			request := f.await(t, f.allowed(t, "test_echo", `{"text": "test text"}`), action.StateFailed)
			if kind := errorKind(t, request); kind != test.kind {
				t.Errorf("kind %q, want %q", kind, test.kind)
			}
			if len(before.made()) != 0 || len(after.made()) != 0 {
				t.Error("a tool was called")
			}
			if test.kind == "mcp_invalid_schema" {
				awaitGroup(t, s.groups, unavailableFor(
					action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_INVALID_CATALOG, anyDetail,
				))
			}
		})
	}
}

func TestASessionThatFailsUnderACallEndsItUnknownAndIsReplaced(t *testing.T) {
	g := newGate()
	defer g.open()
	// sessions are the sessions the tools were called on.
	sessions := make(chan string, 4)
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_hang", InputSchema: objectSchema},
		func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			sessions <- request.Session.ID()
			g.hold(ctx)
			return textResult("test late answer"), nil
		})
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: textSchema},
		func(_ context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			sessions <- request.Session.ID()
			return textResult("test text"), nil
		})
	f, u, s := offered(t, server, nil, executorTiming(), "test_echo", "test_hang")

	id := f.allowed(t, "test_hang", `{}`)
	g.awaitStarted(t)
	// The call's connection breaks: the server may have run the tool, so the Execution cannot tell and is never
	// replayed.
	u.http.CloseClientConnections()
	if kind := errorKind(t, f.await(t, id, action.StateExecutionUnknown)); kind != string(store.UnknownAdapterOutcome) {
		t.Errorf("kind %q", kind)
	}
	// The server closes the failed session once the tool it still runs returns.
	g.open()
	awaitGroup(t, s.groups, offering("test_echo", "test_hang"))
	f.await(t, f.allowed(t, "test_echo", `{"text": "test text"}`), action.StateSucceeded)
	close(sessions)
	var called []string
	for session := range sessions {
		called = append(called, session)
	}
	// Each tool was called once, the second on a session of its own.
	if len(called) != 2 || called[0] == called[1] {
		t.Errorf("the tools were called on the sessions %q", called)
	}
}

func TestALeaseLostWhileTheToolsAreReadStopsTheExecutionBeforeTheCall(t *testing.T) {
	echo := &calls{}
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: textSchema}, answering(echo, textResult("test text")))
	f, u, _ := offered(t, server, nil, executorTiming(), "test_echo")

	listing := u.holdNextList()
	defer listing.open()
	id := f.allowed(t, "test_echo", `{"text": "test text"}`)
	listing.awaitStarted(t)
	// The lease lapses while the Execution reads the tools: the sweep ends it as unknown. The worker's lease outlasts
	// the test, so only the renewal before the call can find the lease gone.
	if _, err := f.db.ExecContext(t.Context(),
		`UPDATE action_execution SET lease_expires_at = now() WHERE request_id = $1`, id); err != nil {
		t.Fatal(err)
	}
	f.await(t, id, action.StateExecutionUnknown)
	listing.open()
	// The worker records the Execution's own outcome over the sweep's once Execute has returned.
	request := f.awaitRequest(t, id, func(request store.ActionRequest) bool { return request.Execution.ReconciledAt != nil })
	if request.State != action.StateExecutionUnknown || len(echo.made()) != 0 {
		t.Errorf("the request is %s and the tool was called %d times", request.State, len(echo.made()))
	}
}

func TestAnExecutionPastItsDeadlineEndsUnknown(t *testing.T) {
	g := newGate()
	defer g.open()
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_hang", InputSchema: objectSchema},
		func(ctx context.Context, _ *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			g.hold(ctx)
			return textResult("test late answer"), nil
		})
	timing := executorTiming()
	timing.Execution = 500 * time.Millisecond
	f, _, _ := offered(t, server, nil, timing, "test_hang")

	// The lease renews for as long as the call runs: only the deadline ends it.
	request := f.await(t, f.allowed(t, "test_hang", `{}`), action.StateExecutionUnknown)
	if kind := errorKind(t, request); kind != string(store.UnknownAdapterOutcome) {
		t.Errorf("kind %q", kind)
	}
}

func TestADrainLetsTheCallInFlightFinishWhileItsLeaseAndItsExecutorAreKeptAlive(t *testing.T) {
	echo := &calls{}
	g := newGate()
	defer g.open()
	server := newServer()
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: textSchema},
		func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			echo.record(request.Params.Arguments)
			g.hold(ctx)
			return textResult("test text"), nil
		})
	f := newFixture(t)
	u := newUpstream(t, server, nil)
	groups := mcpGroup(t, httpConfig(u.url()))
	s := supervise(t, groups, nil, executorTiming())
	awaitGroup(t, groups, offering("test_echo"))
	timing := workerTiming()
	timing.Lease = 600 * time.Millisecond
	timing.Drain = awaitBound
	w := f.work(t, groups, s.Executor, timing)

	id := f.allowed(t, "test_echo", `{"text": "test text"}`)
	g.awaitStarted(t)
	var claimed time.Time
	if err := f.db.QueryRowContext(t.Context(), `SELECT lease_expires_at FROM action_execution WHERE request_id = $1`,
		id).Scan(&claimed); err != nil {
		t.Fatal(err)
	}
	drained := time.Now()
	w.drain()
	s.stop()

	// The call outlives the lease it was claimed with, and the executor keeps proving itself alive.
	eventually(t, func() (bool, string) {
		var expires, heartbeat time.Time
		if err := f.db.QueryRowContext(t.Context(), `
			SELECT
				(SELECT lease_expires_at FROM action_execution WHERE request_id = $1),
				(SELECT max(heartbeat_at) FROM action_executor_heartbeat)`, id).Scan(&expires, &heartbeat); err != nil {
			t.Fatal(err)
		}
		renewed := time.Now().After(claimed) && expires.After(claimed)
		alive := heartbeat.After(drained)
		return renewed && alive, fmt.Sprintf("lease renewed past its window: %t; executor heartbeat since: %t", renewed, alive)
	})
	if expired, err := f.store.ExpireStaleLeases(t.Context(), time.Second); err != nil || len(expired) != 0 {
		t.Errorf("the sweep ended %v, %v", expired, err)
	}
	if request := f.get(t, id); request.State != action.StateRunning {
		t.Errorf("the request is %s", request.State)
	}
	health := group(t, groups).Health
	if health.State != action_servicepb.McpLifecycle_MCP_LIFECYCLE_DRAINING {
		t.Errorf("health %v", health)
	}
	select {
	case <-s.done:
		t.Fatal("the executor stopped under the call")
	default:
	}

	g.open()
	request := f.await(t, id, action.StateSucceeded)
	sameJSON(t, request.Execution.Result, json.RawMessage(`{"content": [{"type": "text", "text": "test text"}]}`))
	if len(echo.made()) != 1 {
		t.Errorf("the tool was called %d times", len(echo.made()))
	}
	select {
	case <-s.done:
	case <-time.After(awaitBound):
		t.Fatal("the executor did not stop once the call ended")
	}
	if health := group(t, groups).Health; health.State != action_servicepb.McpLifecycle_MCP_LIFECYCLE_STOPPED {
		t.Errorf("health %v", health)
	}
	if ended := u.ended.Load(); ended != 1 {
		t.Errorf("the session was ended %d times once the executor stopped", ended)
	}
}
