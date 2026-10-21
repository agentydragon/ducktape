// Package mcpexec runs the Actions of the ActionGroups bound to upstream MCP servers. The Executor of a group keeps a
// session with its server, offers the server's tools as the group's Actions, and calls a tool for each Execution: no
// caller gets an MCP client of its own. An OAuth-backed group stays unavailable until an operator's linkage holds a
// token for its server.
package mcpexec

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/sethvargo/go-retry"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Timing is an Executor's intervals and bounds.
type Timing struct {
	// Refresh is how often an offered group's tools are read again when the server does not say they changed.
	Refresh time.Duration
	// Lifecycle bounds connecting, one read of the tools and one read of the linkage.
	Lifecycle time.Duration
	// Execution bounds one Execution, since renewing its lease proves this service alive, not the server: a call the
	// server stops answering would otherwise be renewed for ever.
	Execution time.Duration
	// Stable is how long a group stays offered before its failures are forgotten, so a server that fails again soon
	// after it came back keeps backing off.
	Stable time.Duration
}

// DefaultTiming is the Python service's.
func DefaultTiming() Timing {
	return Timing{Refresh: 5 * time.Minute, Lifecycle: 15 * time.Second, Execution: 10 * time.Minute, Stable: 30 * time.Second}
}

// backoff paces the attempts after a failure: jittered, doubling from half a second to thirty.
func backoff() retry.Backoff {
	return retry.WithCappedDuration(30*time.Second, retry.WithJitterPercent(50, retry.NewExponential(500*time.Millisecond)))
}

// detailLimit bounds a health detail, whose text the server chooses.
const detailLimit = 2000

// Executor runs the Actions of one MCP group on its server: the worker.Executor of the group, and the supervisor of
// its session (Run).
type Executor struct {
	key      string
	groups   *catalog.Catalog
	server   server
	linkages Linkages
	timing   Timing
	client   *mcp.Client
	// wakeups asks the supervisor to try again now: the tools or the linkage changed, or a session failed under an
	// Execution.
	wakeups chan struct{}

	mu sync.Mutex
	// current is the session Executions are pinned to; nil while there is none.
	current   *session
	available bool
	// actions are the offered tools' definitions, as the catalog holds them.
	actions map[string]catalog.ActionDefinition
	// state, reason, detail, discovered, retryAt and failures are the group's health; see action_servicepb.McpHealth.
	// A zero time is none.
	state      action_servicepb.McpLifecycle
	reason     *action_servicepb.McpUnavailableReason
	detail     *string
	discovered time.Time
	retryAt    time.Time
	failures   int
	// availableSince is when the group was last offered; zero while it is not.
	availableSince time.Time
	backoff        retry.Backoff
	draining       bool
	// retiring are the sessions replaced or drained, until they close.
	retiring sync.WaitGroup
}

// session is one session with the server.
type session struct {
	client      *mcp.ClientSession
	connectedAt time.Time
	// calls counts the Executions pinned to it. It closes once none is after it is replaced.
	calls sync.WaitGroup
	// failed is whether a request on it failed; the supervisor then replaces it. Guarded by the Executor's mu.
	failed bool
}

// New is the Executor of the catalog's group key, which binding binds to an MCP server. It withdraws the group's
// Actions until its server lists them; each change to the group is an Update of groups. linkages read the token of a
// server its config authorizes through an operator's OAuth linkage.
//
// A config the Python service refuses is refused here, and its error names fields, never a value.
func New(groups *catalog.Catalog, key string, binding catalog.MCPBinding, linkages Linkages, timing Timing) (*Executor, error) {
	server, err := parseServer(binding.Config)
	if err != nil {
		return nil, fmt.Errorf("ActionGroup %s has an invalid MCP binding: %w", key, err)
	}
	if _, linked := linkedAccountOf(server); linked && linkages == nil {
		return nil, fmt.Errorf("ActionGroup %s is authorized by an OAuth linkage, and no linkages were given", key)
	}
	if timing.Refresh <= 0 || timing.Lifecycle <= 0 || timing.Execution <= 0 || timing.Stable < 0 {
		return nil, fmt.Errorf("MCP executor intervals must be positive: %+v", timing)
	}
	e := &Executor{
		key: key, groups: groups, server: server, linkages: linkages, timing: timing,
		wakeups: make(chan struct{}, 1), backoff: backoff(), state: action_servicepb.McpLifecycle_MCP_LIFECYCLE_DISCONNECTED,
	}
	e.client = mcp.NewClient(&mcp.Implementation{Name: "agentplane-action-service"}, &mcp.ClientOptions{
		ToolListChangedHandler: func(context.Context, *mcp.ToolListChangedRequest) { e.wake() },
	})
	e.mu.Lock()
	defer e.mu.Unlock()
	e.publish()
	return e, nil
}

// Run supervises the group's session until ctx ends. It connects, offers the server's tools as the group's Actions,
// and reads them again every Timing.Refresh and whenever the server says they changed. After a failure it withdraws
// the Actions and tries again with backoff, so a server that is down leaves its group unavailable and nothing else:
// the service serves its other groups meanwhile. Once ctx ends the group drains: Run withdraws it, and returns when
// the Executions it holds have ended and its sessions are closed. Call Run once.
func (e *Executor) Run(ctx context.Context) {
	for ctx.Err() == nil {
		select {
		case <-e.wakeups:
		default:
		}
		e.attempt(ctx)
		if ctx.Err() == nil {
			e.sleep(ctx, e.schedule())
		}
	}
	e.mu.Lock()
	e.draining = true
	e.withdraw(nil, "")
	e.state, e.retryAt = action_servicepb.McpLifecycle_MCP_LIFECYCLE_DRAINING, time.Time{}
	e.retire()
	e.publish()
	e.mu.Unlock()

	e.retiring.Wait()
	e.mu.Lock()
	defer e.mu.Unlock()
	e.state = action_servicepb.McpLifecycle_MCP_LIFECYCLE_STOPPED
	e.publish()
}

// attempt brings the group to where its server and linkage let it be now.
func (e *Executor) attempt(ctx context.Context) {
	e.mu.Lock()
	e.retryAt = time.Time{}
	e.mu.Unlock()
	if account, linked := linkedAccountOf(e.server); linked {
		reading, cancel := context.WithTimeout(ctx, e.timing.Lifecycle)
		problem, err := linkageProblem(reading, e.linkages, account.serverID)
		cancel()
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			// Database errors can carry request material; the type names the failure.
			slog.Warn("MCP OAuth linkage read failed; the group stays unavailable", "group", e.key,
				"error_type", fmt.Sprintf("%T", err))
			problem = "reading the OAuth linkage failed: " + e.describe(err)
		}
		if problem != "" {
			e.mu.Lock()
			defer e.mu.Unlock()
			e.withdraw(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_LINKAGE_UNAVAILABLE.Enum(), problem)
			e.retire()
			return
		}
	}
	e.mu.Lock()
	if e.current != nil && e.current.failed {
		e.retire()
	}
	s := e.current
	e.mu.Unlock()
	if s == nil {
		var err error
		if s, err = e.connect(ctx); err != nil {
			if ctx.Err() == nil {
				e.mu.Lock()
				defer e.mu.Unlock()
				e.withdraw(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_CONNECT_FAILED.Enum(), e.describe(err))
			}
			return
		}
	}
	e.refresh(ctx, s)
}

func (e *Executor) connect(ctx context.Context) (*session, error) {
	e.mu.Lock()
	e.state = action_servicepb.McpLifecycle_MCP_LIFECYCLE_CONNECTING
	e.publish()
	e.mu.Unlock()
	transport, err := e.server.transport(e.linkages, e.timing.Lifecycle)
	if err != nil {
		return nil, err
	}
	connecting, cancel := context.WithTimeout(ctx, e.timing.Lifecycle)
	defer cancel()
	// The session outlives connecting's deadline: go-sdk ties only the handshake to it.
	client, err := e.client.Connect(connecting, transport, nil)
	if err != nil {
		return nil, err
	}
	s := &session{client: client, connectedAt: time.Now()}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.current = s
	return s, nil
}

// refresh reads the server's tools on s and offers them, unless s failed meanwhile.
func (e *Executor) refresh(ctx context.Context, s *session) {
	e.mu.Lock()
	e.state = action_servicepb.McpLifecycle_MCP_LIFECYCLE_DISCOVERING
	e.publish()
	e.mu.Unlock()
	listing, cancel := context.WithTimeout(ctx, e.timing.Lifecycle)
	tools, err := e.list(listing, s)
	cancel()
	if ctx.Err() != nil {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	var invalid *invalidCatalogError
	switch {
	case errors.As(err, &invalid):
		// The session is sound; the next attempt reads its tools again.
		e.withdraw(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_INVALID_CATALOG.Enum(), invalid.Error())
	case err != nil:
		e.fail(s, action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_DISCOVERY_FAILED, err)
	case !s.failed:
		e.offer(tools)
	}
}

func (e *Executor) list(ctx context.Context, s *session) (map[string]tool, error) {
	var listed []*mcp.Tool
	for tool, err := range s.client.Tools(ctx, nil) {
		if err != nil {
			return nil, err
		}
		listed = append(listed, tool)
	}
	return mirror(listed)
}

// schedule is how long the supervisor waits before its next attempt: the refresh interval while the group is offered,
// else the next backoff, which it records in the group's health.
func (e *Executor) schedule() time.Duration {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.available {
		return e.timing.Refresh
	}
	e.failures++
	delay, _ := e.backoff.Next()
	e.retryAt = time.Now().Add(delay)
	e.publish()
	reason := "none"
	if e.reason != nil {
		reason = e.reason.String()
	}
	slog.Warn("MCP group unavailable; retry scheduled", "group", e.key, "reason", reason, "retry_in", delay)
	return delay
}

// sleep waits for d, a wakeup or the end of ctx.
func (e *Executor) sleep(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-e.wakeups:
	case <-timer.C:
	}
}

// LinkageChanged tells a group authorized by an OAuth linkage that its linkage may have changed: the supervisor reads
// it again now, so a linked, refreshed or lost account takes effect without waiting for the next attempt. Any other
// group ignores it.
func (e *Executor) LinkageChanged() {
	if _, linked := linkedAccountOf(e.server); linked {
		e.wake()
	}
}

func (e *Executor) wake() {
	select {
	case e.wakeups <- struct{}{}:
	default:
	}
}

// offer makes tools the group's Actions. Callers hold mu.
func (e *Executor) offer(tools map[string]tool) {
	now := time.Now()
	e.actions = make(map[string]catalog.ActionDefinition, len(tools))
	for name, tool := range tools {
		e.actions[name] = tool.definition
	}
	e.available = true
	if e.availableSince.IsZero() {
		e.availableSince = now
	}
	e.forgetStableFailures(now)
	e.state, e.reason, e.detail, e.retryAt, e.discovered = action_servicepb.McpLifecycle_MCP_LIFECYCLE_AVAILABLE, nil, nil, time.Time{}, now
	e.publish()
}

// withdraw takes the group's Actions away, for reason, which detail says more of; a nil reason gives none. Callers
// hold mu.
func (e *Executor) withdraw(reason *action_servicepb.McpUnavailableReason, detail string) {
	e.forgetStableFailures(time.Now())
	e.actions, e.available, e.availableSince = nil, false, time.Time{}
	e.state, e.reason, e.detail = action_servicepb.McpLifecycle_MCP_LIFECYCLE_DISCONNECTED, reason, nil
	if detail != "" {
		if utf8.RuneCountInString(detail) > detailLimit {
			detail = string([]rune(detail)[:detailLimit])
		}
		e.detail = &detail
	}
	e.publish()
}

// forgetStableFailures forgets the failures before a stay offered long enough to call stable. Callers hold mu.
func (e *Executor) forgetStableFailures(now time.Time) {
	if !e.availableSince.IsZero() && now.Sub(e.availableSince) >= e.timing.Stable {
		e.failures = 0
		e.backoff = backoff()
	}
}

// fail records that a request on s failed: the supervisor replaces s, and while it is current the group is withdrawn
// for reason. It reports whether this is the failure that withdrew the group. Callers hold mu.
func (e *Executor) fail(s *session, reason action_servicepb.McpUnavailableReason, err error) bool {
	first := !s.failed && e.current == s
	s.failed = true
	if first {
		e.withdraw(reason.Enum(), e.describe(err))
	}
	return first
}

// retire replaces the current session: it closes once the Executions pinned to it end, which Timing.Execution bounds.
// go-sdk's Close waits only for the requests in flight; an Execution also holds the session between its requests.
// Callers hold mu.
func (e *Executor) retire() {
	s := e.current
	if s == nil {
		return
	}
	e.current = nil
	e.retiring.Go(func() {
		s.calls.Wait()
		if err := s.client.Close(); err != nil {
			// The SDK's errors can carry the server's address or a credential; the type names the failure.
			slog.Warn("MCP session close failed", "group", e.key, "error_type", fmt.Sprintf("%T", err))
		}
	})
}

// publish writes the group's state to the catalog. Callers hold mu.
func (e *Executor) publish() {
	health := &action_servicepb.McpHealth{
		State: e.state, Reason: e.reason, Detail: e.detail, LastDiscoveryAt: timestamp(e.discovered),
		RetryAt: timestamp(e.retryAt), Failures: int32(e.failures),
	}
	available, actions := e.available, e.actions
	e.groups.Update(e.key, func(g *catalog.Group) {
		g.Available, g.Actions, g.Health = available, actions, health
	})
}

// timestamp is t, or nil for the zero time.
func timestamp(t time.Time) *timestamppb.Timestamp {
	if t.IsZero() {
		return nil
	}
	return timestamppb.New(t)
}

// describe is err for an operator, a lifecycle step's deadline by its bound.
func (e *Executor) describe(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return fmt.Sprintf("no answer within %s", e.timing.Lifecycle)
	}
	return err.Error()
}

// Execute calls the tool request names, on the session the group offered it from. It reads the server's tools first:
// the arguments were admitted against the schema the group offered, and a tool whose schema changed since is not
// called with them. A call whose outcome the server leaves uncertain, because the session failed under it or the
// tool says so, ends execution_unknown; a tool's error result is its answer, and succeeds.
func (e *Executor) Execute(ctx context.Context, request store.ExecutionRequest, lease *worker.Lease) (store.ExecutionOutcome, error) {
	s := e.pin()
	if s == nil {
		return unavailable(nil), nil
	}
	defer s.calls.Done()
	bounded, cancel := context.WithTimeout(ctx, e.timing.Execution)
	defer cancel()
	outcome, err := worker.Hold(bounded, lease, func(ctx context.Context) (store.ExecutionOutcome, error) {
		return e.call(ctx, request, lease, s)
	})
	if err != nil && ctx.Err() == nil && errors.Is(bounded.Err(), context.DeadlineExceeded) {
		return store.ExecutionOutcome{}, fmt.Errorf("%w: MCP execution deadline exceeded", worker.ErrOutcomeUnknown)
	}
	return outcome, err
}

// pin is the session an Execution may call on, which then waits for the Execution before it closes; nil while the
// group is not offered.
func (e *Executor) pin() *session {
	e.mu.Lock()
	defer e.mu.Unlock()
	s := e.current
	if s == nil || s.failed || !e.available || e.draining {
		return nil
	}
	s.calls.Add(1)
	return s
}

func (e *Executor) call(ctx context.Context, request store.ExecutionRequest, lease *worker.Lease, s *session) (store.ExecutionOutcome, error) {
	name := request.Action.Name
	started := time.Now()
	listing, cancel := context.WithTimeout(ctx, e.timing.Lifecycle)
	tools, err := e.list(listing, s)
	cancel()
	var invalid *invalidCatalogError
	switch {
	case ctx.Err() != nil:
		return store.ExecutionOutcome{}, ctx.Err()
	case errors.As(err, &invalid):
		e.mu.Lock()
		current := e.current == s
		if current {
			e.withdraw(action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_INVALID_CATALOG.Enum(), invalid.Error())
		}
		e.mu.Unlock()
		if current {
			e.wake()
		}
		return worker.Failed("mcp_invalid_schema", "backend tool schema is invalid"), nil
	case err != nil:
		diagnostics := map[string]any{
			"exception_type":     fmt.Sprintf("%T", err),
			"connection_age_s":   seconds(started.Sub(s.connectedAt)),
			"request_duration_s": seconds(time.Since(started)),
		}
		// The SDK's errors can carry the server's address or a credential, so their text is not logged.
		slog.Warn("MCP execution schema check failed", "group", e.key, "diagnostics", diagnostics)
		e.sessionFailed(s, err)
		return unavailable(diagnostics), nil
	}
	tool, offered := tools[name]
	if !offered {
		return worker.Failed("unknown_action", "action is no longer offered by the backend"), nil
	}
	if !tool.matches(request.Arguments) {
		return worker.Failed("incompatible_action_schema", "arguments no longer match the current tool schema"), nil
	}
	if err := lease.Renew(ctx); err != nil {
		return store.ExecutionOutcome{}, err
	}
	e.mu.Lock()
	usable := e.current == s && !s.failed
	e.mu.Unlock()
	if !usable {
		return unavailable(nil), nil
	}
	result, err := s.client.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: request.Arguments})
	if err != nil {
		if ctx.Err() != nil {
			return store.ExecutionOutcome{}, ctx.Err()
		}
		e.sessionFailed(s, err)
		return store.ExecutionOutcome{}, fmt.Errorf("%w: MCP tools/call transport failure for %s", worker.ErrOutcomeUnknown, name)
	}
	if reportsUnknownOutcome(result) {
		return store.ExecutionOutcome{}, fmt.Errorf("%w: MCP backend reported an unknown execution outcome", worker.ErrOutcomeUnknown)
	}
	stored, err := storedResult(result)
	if err != nil {
		return store.ExecutionOutcome{}, fmt.Errorf("%w: the MCP result cannot be stored (%T)", worker.ErrOutcomeUnknown, err)
	}
	return store.ExecutionOutcome{State: store.ExecutionSucceeded, Result: stored}, nil
}

// sessionFailed records that a request of an Execution on s failed, and wakes the supervisor to replace s.
func (e *Executor) sessionFailed(s *session, err error) {
	e.mu.Lock()
	first := e.fail(s, action_servicepb.McpUnavailableReason_MCP_UNAVAILABLE_REASON_SESSION_FAILED, err)
	e.mu.Unlock()
	if first {
		e.wake()
	}
}

// seconds is d in seconds, to the millisecond.
func seconds(d time.Duration) float64 {
	return math.Round(d.Seconds()*1000) / 1000
}

// unavailable is the failure of an Execution the group cannot run now, with diagnostics of the request that showed
// it when there are any.
func unavailable(diagnostics map[string]any) store.ExecutionOutcome {
	return worker.ErrorDocument{
		Kind: "mcp_unavailable", Message: "could not verify the current tool schema", Diagnostics: diagnostics,
	}.Outcome(store.ExecutionFailed)
}
