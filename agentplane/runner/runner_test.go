package runner

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestOpenSessionReservesLazilyAndIsIdempotent(t *testing.T) {
	var lookups atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lookups.Add(1)
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	defer proxy.Close()
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	opened, err := r.OpenSession(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if opened.Attached == nil || opened.Attached.LastCursor != 0 {
		t.Fatalf("initial reservation = %v", opened.Attached)
	}
	if _, err := os.Stat(filepath.Join(r.config.StateDir, "sessions", "session-1", "journal.sqlite")); err != nil {
		t.Fatalf("reservation journal was not created: %v", err)
	}
	statusResponse, err := r.GetStatus(context.Background(), &runnerpb.GetStatusRequest{})
	if err != nil || len(statusResponse.Processes) != 0 {
		t.Fatalf("open started a native process: status=%v err=%v", statusResponse, err)
	}
	if _, err := r.OpenSession(context.Background(), request); err != nil {
		t.Fatalf("identical open was not idempotent: %v", err)
	}
	if got := lookups.Load(); got != 1 {
		t.Fatalf("model config lookup count = %d, want one", got)
	}
	conflict := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	conflict.Spec.Model = "different-model"
	if _, err := r.OpenSession(context.Background(), conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflicting reservation error = %v, want AlreadyExists", err)
	}
	if got := lookups.Load(); got != 1 {
		t.Fatalf("conflicting reservation queried model config; lookups=%d", got)
	}
}

func TestSessionEnvironmentCarriesItsOwnDestination(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.HarnessEnvironment = map[string]string{"TZ": "UTC"}
	r := newLifecycleRunner(t, config)
	want := map[string]string{
		"session-1": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-1"}`,
		"session-2": `{"destination_ref":{"namespace":"test","name":"sandbox","uid":"sandbox-uid"},"session_id":"session-2"}`,
	}
	for id := range want {
		if _, err := r.OpenSession(context.Background(), openLifecycleSession(id, filepath.Join(workspace, "project"))); err != nil {
			t.Fatal(err)
		}
	}
	for id, destination := range want {
		session, err := r.load(context.Background(), id)
		if err != nil {
			t.Fatal(err)
		}
		environment := session.Session().Environment
		if environment["AGENTPLANE_DESTINATION"] != destination {
			t.Fatalf("%s environment = %v, want its destination %s", id, environment, destination)
		}
	}
	if _, shared := r.config.HarnessEnvironment["AGENTPLANE_DESTINATION"]; shared {
		t.Fatal("a Session's destination leaked into the runner-wide harness environment")
	}
}

func TestInvalidInputAndStaleTurnNeverAdmitCommands(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.OpenSession(context.Background(), openLifecycleSession("bad/session", workspace)); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid session ID error = %v, want InvalidArgument", err)
	}
	if _, err := r.OpenSession(context.Background(), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SubmitInput(context.Background(), &runnerservicepb.SubmitInputRequest{SessionId: "session-1", Text: "hello"}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty command ID error = %v, want InvalidArgument", err)
	}
	if _, err := r.InterruptTurn(context.Background(), &runnerservicepb.InterruptTurnRequest{SessionId: "session-1", CommandId: "stale", TurnId: "not-active"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale interrupt error = %v, want FailedPrecondition", err)
	}
	session := r.sessions["session-1"]
	if cursor := session.journal.LastCursor(); cursor != 0 {
		t.Fatalf("rejected commands advanced journal to %d", cursor)
	}
	if record, err := session.journal.GetCommand(context.Background(), "stale"); err != nil || record != nil {
		t.Fatalf("stale command record = %v, %v", record, err)
	}
}

func TestStartupFailureSettlesAdmissionAndReturnsToIdle(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, filepath.Join(t.TempDir(), "missing-harness")))
	if _, err := r.OpenSession(context.Background(), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SubmitInput(context.Background(), &runnerservicepb.SubmitInputRequest{SessionId: "session-1", CommandId: "input-1", Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	session := r.sessions["session-1"]
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		changed := r.changesSince()
		record, err := session.journal.GetCommand(ctx, "input-1")
		if err != nil {
			t.Fatal(err)
		}
		facts, err := r.facts()
		if err != nil {
			t.Fatal(err)
		}
		if record != nil && record.TerminalCursor != nil && facts.IdleSince != nil {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatalf("startup did not settle admitted work: %v", ctx.Err())
		case <-changed:
		}
	}
	session.mu.Lock()
	failure := session.failure
	session.mu.Unlock()
	if failure == nil {
		t.Fatal("failed launch did not publish a failure cursor")
	}
	entries, err := session.journal.Since(ctx, 0, 16)
	if err != nil {
		t.Fatal(err)
	}
	var commandFailed, launchFailed bool
	for _, entry := range entries {
		commandFailed = commandFailed || entry.GetEvent().GetCommandFailed() != nil
		launchFailed = launchFailed || entry.GetEvent().GetHarnessLaunchFailed() != nil
	}
	if !commandFailed || !launchFailed {
		t.Fatalf("failed startup did not settle both work and launch: command=%t launch=%t", commandFailed, launchFailed)
	}
}

func TestRunnerFactsExposeBusyReasonsAndReturnToIdle(t *testing.T) {
	// Facts project a supplied snapshot. Do not run controllers that can replace
	// synthetic runtime values while the test switches from busy to idle.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store, err := NewSessionStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r := &Runner{ctx: ctx, store: store, fatal: make(chan error, 1)}
	r.lifecycle, err = newLifecycle(r)
	if err != nil {
		t.Fatal(err)
	}
	session := &Session{runner: r, id: "session-1", pending: map[string]bool{}}
	r.sessions = map[string]*Session{session.id: session}
	if err := r.lifecycle.state.Create(ctx, lifecycleResourceFor(session.id, sessionRuntimeSpec{}), state.WithCreateOwner(sessionControllerName)); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.state.Create(ctx, lifecycleResourceFor("bootstrap", bootstrapRuntimeSpec{}), state.WithCreateOwner(bootstrapControllerName)); err != nil {
		t.Fatal(err)
	}
	bootstrapScript := "unused test bootstrap"
	r.config.Launch.Bootstrap = &bootstrapScript
	setBootstrapRuntime := func(phase bootstrapPhase) error {
		ctx, cancel := context.WithTimeout(r.ctx, lifecycleTestTimeout)
		defer cancel()
		return modifyLifecycleForTest(ctx, r, "bootstrap", bootstrapControllerName, func(current *bootstrapRuntimeSpec) {
			current.Phase = phase
		})
	}
	if err := setBootstrapRuntime(bootstrapRunning); err != nil {
		t.Fatal(err)
	}
	if err := publishLifecycle(r.lifecycle, "bootstrap", bootstrapInputSpec{Requested: true}); err != nil {
		t.Fatal(err)
	}
	resetManualState := func() {
		r.mu.Lock()
		r.admissions = 0
		r.mu.Unlock()
		session.mu.Lock()
		session.activeTurn = ""
		session.pending = map[string]bool{}
		session.setup = runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED
		session.mu.Unlock()
		if err := setSessionRuntimePhaseForTest(r, session, executionDormant); err != nil {
			t.Errorf("restore session runtime: %v", err)
		}
		if err := setBootstrapRuntime(bootstrapSucceeded); err != nil {
			t.Errorf("restore bootstrap runtime: %v", err)
		}
		if err := publishLifecycle(r.lifecycle, "bootstrap", bootstrapInputSpec{}); err != nil {
			t.Errorf("restore bootstrap input: %v", err)
		}
	}

	session.mu.Lock()
	session.activeTurn = "turn-1"
	session.pending["command-1"] = true
	session.setup = runnerpb.SetupState_SETUP_STATE_RUNNING
	session.mu.Unlock()
	setRuntimePhase(t, session, executionPreparing)
	r.mu.Lock()
	r.admissions = 1
	r.mu.Unlock()

	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, blocker := range facts.AutosuspendBlockers {
		if blocker.SessionId == nil {
			got = append(got, blocker.Reason)
		} else {
			got = append(got, *blocker.SessionId+":"+blocker.Reason)
		}
	}
	want := []string{
		"session-1:Activation", "session-1:ActiveTurn", "session-1:PendingCommand", "session-1:Setup",
		"Admission", "Bootstrap",
	}
	if !reflect.DeepEqual(got, want) || !facts.SetupsRunning || facts.IdleSince != nil {
		t.Fatalf("busy facts = (%v, setups=%t, idleSince=%v), want (%v, true, nil)", got, facts.SetupsRunning, facts.IdleSince, want)
	}

	resetManualState()

	facts, err = r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if len(facts.AutosuspendBlockers) != 0 || facts.SetupsRunning || facts.IdleSince == nil {
		t.Fatalf("idle facts = (%v, setups=%t, idleSince=%v), want no blockers, false, and an idle timestamp", facts.AutosuspendBlockers, facts.SetupsRunning, facts.IdleSince)
	}
}

func TestQuiesceDoesNotWaitForModelLookupOrCreateLateSession(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseProxy := func() { releaseOnce.Do(func() { close(release) }) }
	openFinished := false
	openStarted := false
	openDone := make(chan error, 1)
	defer func() {
		releaseProxy()
		if openStarted && !openFinished {
			select {
			case <-openDone:
			case <-time.After(3 * time.Second):
				t.Error("open request did not finish after releasing model lookup")
			}
		}
	}()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	t.Cleanup(proxy.Close)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	openStarted = true
	go func() {
		_, err := r.OpenSession(context.Background(), openLifecycleSession("late-session", filepath.Join(workspace, "project")))
		openDone <- err
	}()
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("model lookup did not start")
	}
	quiesceCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	quiesced := make(chan error, 1)
	go func() {
		_, err := r.Quiesce(quiesceCtx, &runnerpb.QuiesceRequest{})
		quiesced <- err
	}()
	select {
	case err := <-quiesced:
		if err != nil {
			t.Fatalf("quiesce failed while lookup was blocked: %v", err)
		}
	case <-quiesceCtx.Done():
		t.Fatal("quiesce waited for an in-flight model-config lookup")
	}
	releaseProxy()
	if err := <-openDone; status.Code(err) != codes.Aborted {
		t.Fatalf("late session creation error = %v, want Aborted", err)
	}
	openFinished = true
	if exists, err := r.store.Exists("late-session"); err != nil || exists {
		t.Fatalf("late session was persisted: exists=%t err=%v", exists, err)
	}
}

func TestBootstrapAndSetupCompleteOnCloseAndDoNotRerun(t *testing.T) {
	stateDir := t.TempDir()
	workspace := t.TempDir()
	bootstrapStarted := filepath.Join(stateDir, "bootstrap-started")
	bootstrapRelease := filepath.Join(stateDir, "bootstrap-release")
	bootstrapCount := filepath.Join(stateDir, "bootstrap-count")
	setupStarted := filepath.Join(stateDir, "setup-started")
	setupRelease := filepath.Join(stateDir, "setup-release")
	setupCount := filepath.Join(stateDir, "setup-count")
	bootstrap := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf b >> %s", shellQuote(bootstrapStarted), shellQuote(bootstrapRelease), shellQuote(bootstrapCount))
	setup := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf s >> %s", shellQuote(setupStarted), shellQuote(setupRelease), shellQuote(setupCount))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &bootstrap, "/bin/false")
	config.StateDir = stateDir
	r, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.AdoptIncarnation("test-incarnation"); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	closeStarted, closeFinished, closed := false, false, false
	closeDone := make(chan error, 1)
	defer func() {
		_ = os.WriteFile(bootstrapRelease, nil, 0o600)
		_ = os.WriteFile(setupRelease, nil, 0o600)
		if closeStarted && !closeFinished {
			select {
			case err := <-closeDone:
				closeFinished = true
				if err != nil {
					t.Error(err)
				}
			case <-time.After(5 * time.Second):
				t.Error("runner did not finish closing after releasing one-shot scripts")
			}
		} else if !closeStarted && !closed {
			if err := r.Close(); err != nil {
				t.Error(err)
			}
		}
	}()
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	request.SetupScript = &setup
	if _, err := r.OpenSession(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, bootstrapStarted)
	closeStarted = true
	go func() { closeDone <- r.Close() }()
	assertStillClosing(t, closeDone, "bootstrap")
	if err := os.WriteFile(bootstrapRelease, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, setupStarted)
	assertStillClosing(t, closeDone, "setup")
	if err := os.WriteFile(setupRelease, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-closeDone:
		closeFinished = true
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("orderly shutdown did not wait for bootstrap and setup to complete")
	}
	closed = true
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("%s after orderly shutdown = %q, %v; want %q", path, data, err, want)
		}
	}

	config.HTTPClient = proxy.Client()
	restarted, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := restarted.Close(); err != nil {
			t.Error(err)
		}
	}()
	if err := restarted.store.AdoptIncarnation("incarnation-next"); err != nil {
		t.Fatal(err)
	}
	if err := restarted.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := restarted.OpenSession(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("%s after restart = %q, %v; want %q", path, data, err, want)
		}
	}
	statusResponse, err := restarted.ListSessions(context.Background(), &runnerpb.ListSessionsRequest{})
	if err != nil || len(statusResponse.Sessions) != 1 || statusResponse.Sessions[0].SetupState != runnerpb.SetupState_SETUP_STATE_SUCCEEDED {
		t.Fatalf("restarted setup state = %v, %v", statusResponse, err)
	}
	waitLifecycle(t, restarted, restarted.bootstrapSettled, "reconstructed bootstrap outcome")
	facts, err := restarted.facts()
	if err != nil || facts.Bootstrap != controlpb.RunnerFacts_BOOTSTRAP_SUCCEEDED {
		t.Fatalf("restarted bootstrap state = %v, %v", facts, err)
	}
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", path)
}

func assertStillClosing(t *testing.T, closed <-chan error, stage string) {
	t.Helper()
	select {
	case err := <-closed:
		t.Fatalf("runner close returned before %s completed: %v", stage, err)
	case <-time.After(100 * time.Millisecond):
	}
}

func lifecycleConfig(t *testing.T, endpoint, workspace string, bootstrap *string, binary string) Config {
	t.Helper()
	return Config{
		StateDir:      filepath.Join(t.TempDir(), "state"),
		Launch:        SandboxLaunch{Sandbox: SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}, Bootstrap: bootstrap},
		WorkspaceRoot: workspace, InitializationCwd: workspace,
		Claude:     &harness.LaunchConfig{Binary: binary, BaseURL: endpoint, Token: "test-token"},
		HTTPClient: http.DefaultClient,
	}
}

func modelConfigServer(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	t.Cleanup(server.Close)
	return server
}

func newLifecycleRunner(t *testing.T, config Config) *Runner {
	t.Helper()
	r, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.AdoptIncarnation("test-incarnation"); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Errorf("close test runner: %v", err)
		}
	})
	return r
}

func openLifecycleSession(sessionID, cwd string) *runnerservicepb.OpenSessionRequest {
	return &runnerservicepb.OpenSessionRequest{
		SessionId: sessionID,
		Spec:      &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model"},
	}
}

func TestScriptOutputFailureStopsProcessAndReturnsCause(t *testing.T) {
	r := newBareRunner(t)
	failure := fmt.Errorf("bootstrap output storage failed")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := r.runScript(ctx, "printf output; exec sleep 1000", t.TempDir(), func(bool, []byte) error { return failure })
	if !errors.Is(err, failure) {
		t.Fatalf("output failure was lost: %v", err)
	}
	if ctx.Err() != nil {
		t.Fatalf("script waited for timeout after output failed: %v", err)
	}
}
