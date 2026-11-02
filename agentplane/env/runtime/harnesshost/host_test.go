package harnesshost

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/session"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func closeHostForTest(t *testing.T, r *Host) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return r.Close(ctx)
}

func TestOpenSessionReservesLazilyAndIsIdempotent(t *testing.T) {
	var lookups atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lookups.Add(1)
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	defer proxy.Close()
	workspace := t.TempDir()
	r := newLifecycleHost(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	opened, err := r.OpenSession(hostContext(r, context.Background()), request)
	if err != nil {
		t.Fatal(err)
	}
	if opened.Attached == nil || opened.Attached.LastCursor != 0 {
		t.Fatalf("initial reservation = %v", opened.Attached)
	}
	if _, err := os.Stat(filepath.Join(r.config.StateDir, "sessions", "session-1", "journal.sqlite")); err != nil {
		t.Fatalf("reservation journal was not created: %v", err)
	}
	statusResponse, err := r.GetStatus(context.Background(), &sessionpb.GetStatusRequest{})
	if err != nil || len(statusResponse.Processes) != 0 {
		t.Fatalf("open started a native process: status=%v err=%v", statusResponse, err)
	}
	if _, err := r.OpenSession(hostContext(r, context.Background()), request); err != nil {
		t.Fatalf("identical open was not idempotent: %v", err)
	}
	if got := lookups.Load(); got != 1 {
		t.Fatalf("model config lookup count = %d, want one", got)
	}
	conflict := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	conflict.Spec.Model = "different-model"
	if _, err := r.OpenSession(hostContext(r, context.Background()), conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflicting reservation error = %v, want AlreadyExists", err)
	}
	if got := lookups.Load(); got != 1 {
		t.Fatalf("conflicting reservation queried model config; lookups=%d", got)
	}
}

func TestOpenSessionRetryUsesPersistedSetupAfterSealPublicationFailure(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	failure := errors.New("injected journal seal publication failure")
	resources := &failJournalSealPublicationOnce{
		State:   state.WrapCore(namespaced.NewState(inmem.Build)),
		failure: failure,
	}
	resources.fail.Store(true)
	r, err := newTestHost(lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"), resources)
	if err != nil {
		t.Fatal(err)
	}
	if err := bindHost(r, "test-incarnation"); err != nil {
		t.Fatal(err)
	}
	if err := r.sessions.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := closeHostForTest(t, r); err != nil {
			t.Errorf("close test host: %v", err)
		}
	})

	marker := filepath.Join(t.TempDir(), "setup-runs")
	script := fmt.Sprintf("printf setup >> %s", shellQuote(marker))
	request := openLifecycleSession("session-retry", filepath.Join(workspace, "project"))
	request.SetupScript = &script
	if _, err := r.OpenSession(hostContext(r, context.Background()), request); status.Code(err) != codes.Unavailable {
		t.Fatalf("first OpenSession error = %v, want injected Unavailable", err)
	}
	if resources.fail.Load() {
		t.Fatal("journal seal publication failure was not injected")
	}
	if r.sessions.Get(request.SessionId) != nil {
		t.Fatal("failed OpenSession became visible in the Session registry")
	}
	if _, found, err := session.ReadInput(r.ctx, r.lifecycle.state, request.SessionId); err != nil || found {
		t.Fatalf("failed OpenSession published a Session input: found=%t, err=%v", found, err)
	}
	record, err := r.store.Read(request.SessionId)
	if err != nil || record.SetupScript != script || record.SetupScriptSHA256 != nil {
		t.Fatalf("retained setup = %q, legacy hash = %v, err=%v; want source authority without cached digest", record.SetupScript, record.SetupScriptSHA256, err)
	}
	wrongScript := "exit 1"
	conflict := openLifecycleSession(request.SessionId, filepath.Join(workspace, "project"))
	conflict.SetupScript = &wrongScript
	if _, err := r.OpenSession(hostContext(r, context.Background()), conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflicting setup source error = %v, want AlreadyExists", err)
	}

	request.SetupScript = nil
	if _, err := r.OpenSession(hostContext(r, context.Background()), request); err != nil {
		t.Fatalf("omitted-script OpenSession retry: %v", err)
	}
	s := r.sessions.Get(request.SessionId)
	if s == nil {
		t.Fatal("successful OpenSession retry did not register the Session")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var cursor uint64
	for {
		entries, err := s.EventJournal().Since(ctx, cursor, 128)
		if err != nil {
			t.Fatal(err)
		}
		finished := false
		for _, entry := range entries {
			cursor = entry.Cursor
			if entry.GetEvent().GetSetupInterrupted() != nil {
				t.Fatalf("fresh setup was marked interrupted during retry: %v", entries)
			}
			finished = finished || entry.GetEvent().GetSetupFinished() != nil
		}
		if finished {
			break
		}
		if err := s.EventJournal().WaitBeyond(ctx, cursor); err != nil {
			t.Fatalf("wait for retried setup: %v", err)
		}
	}
	output, err := os.ReadFile(marker)
	if err != nil || string(output) != "setup" {
		t.Fatalf("setup execution marker = %q, err=%v; want exactly one setup run", output, err)
	}
}

func TestRunnerRestartDoesNotReplayRetainedSetup(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.StateDir = filepath.Join(t.TempDir(), "state")
	const sessionID = "setup-restart"
	marker := filepath.Join(t.TempDir(), "setup-ran")
	setupScript := fmt.Sprintf("touch %s", shellQuote(marker))
	spec := &sessionpb.SessionSpec{
		Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: filepath.Join(workspace, "project"), Model: "test-model",
		Instructions: config.Launch.Instructions(sessionID, ""),
	}
	store, err := retained.NewStore(filepath.Join(config.StateDir, "sessions"))
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(spec, setupScript, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write(sessionID, record); err != nil {
		t.Fatal(err)
	}
	directory, err := store.Directory(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	eventJournal, err := journal.Open(context.Background(), filepath.Join(directory, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := eventJournal.Append(context.Background(), &eventpb.Event{
		Observation: &eventpb.Event_SetupStarted{SetupStarted: &eventpb.SetupStarted{}},
	}, journal.AppendOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := eventJournal.Close(); err != nil {
		t.Fatal(err)
	}

	r := newLifecycleHost(t, config)
	request := openLifecycleSession(sessionID, filepath.Join(workspace, "project"))
	if _, err := r.OpenSession(hostContext(r, context.Background()), request); err != nil {
		t.Fatalf("open recovered Session without resupplying setup source: %v", err)
	}
	input, found, err := session.ReadInput(r.ctx, r.lifecycle.state, sessionID)
	if err != nil || !found || input.Setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED || !input.SetupRequested {
		t.Fatalf("recovered setup input = %+v, found=%t, err=%v; want retained but interrupted", input, found, err)
	}
	if err := closeHostForTest(t, r); err != nil {
		t.Fatalf("close recovered Host: %v", err)
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("retained setup source replayed after restart: stat marker err=%v", err)
	}
	eventJournal, err = journal.Open(context.Background(), filepath.Join(directory, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		t.Fatal(err)
	}
	defer eventJournal.Close()
	entries, err := eventJournal.Since(context.Background(), 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	started, interrupted := 0, 0
	for _, entry := range entries {
		if entry.GetEvent().GetSetupStarted() != nil {
			started++
		}
		if entry.GetEvent().GetSetupInterrupted() != nil {
			interrupted++
		}
	}
	if started != 1 || interrupted != 1 {
		t.Fatalf("setup recovery events = %v; want one started and one interrupted, with no replay", entries)
	}
}

func TestInvalidInputAndStaleTurnNeverAdmitCommands(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleHost(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.OpenSession(hostContext(r, context.Background()), openLifecycleSession("bad/session", workspace)); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid session ID error = %v, want InvalidArgument", err)
	}
	if _, err := r.OpenSession(hostContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SubmitInput(hostContext(r, context.Background()), &environmentruntimepb.SubmitInputRequest{SessionId: "session-1", Text: "hello"}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty command ID error = %v, want InvalidArgument", err)
	}
	if _, err := r.InterruptTurn(hostContext(r, context.Background()), &environmentruntimepb.InterruptTurnRequest{SessionId: "session-1", CommandId: "stale", TurnId: "not-active"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale interrupt error = %v, want FailedPrecondition", err)
	}
	session := r.sessions.Get("session-1")
	if cursor := session.EventJournal().LastCursor(); cursor != 0 {
		t.Fatalf("rejected commands advanced journal to %d", cursor)
	}
	if record, err := session.EventJournal().GetCommand(context.Background(), "stale"); err != nil || record != nil {
		t.Fatalf("stale command record = %v, %v", record, err)
	}
}

func TestStartupFailureSettlesAdmissionAndReturnsToIdle(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleHost(t, lifecycleConfig(t, proxy.URL, workspace, nil, filepath.Join(t.TempDir(), "missing-harness")))
	if _, err := r.OpenSession(hostContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SubmitInput(hostContext(r, context.Background()), &environmentruntimepb.SubmitInputRequest{SessionId: "session-1", CommandId: "input-1", Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	session := r.sessions.Get("session-1")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		changed := r.changesSince()
		record, err := session.EventJournal().GetCommand(ctx, "input-1")
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
	statusResponse, err := r.GetStatus(ctx, &sessionpb.GetStatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(statusResponse.Sessions) != 1 || statusResponse.Sessions[0].FailureCursor == nil {
		t.Fatalf("failed launch status did not publish a failure cursor: %v", statusResponse.Sessions)
	}
	entries, err := session.EventJournal().Since(ctx, 0, 16)
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

type initialOperationFailureState struct {
	state.State
	initialFailure  error
	teardownFailure error
	failInitial     atomic.Bool
	failTeardown    atomic.Bool
	failed          chan struct{}
	teardownFailed  chan struct{}
}

type failJournalSealPublicationOnce struct {
	state.State
	failure error
	fail    atomic.Bool
}

func (s *failJournalSealPublicationOnce) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == journalSealType && s.fail.CompareAndSwap(true, false) {
		return s.failure
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func (s *initialOperationFailureState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == session.OperationType {
		desired, found, err := runnerlifecycle.ReadDesired(ctx, s.State)
		if err == nil && found {
			var armed *atomic.Bool
			var failure error
			var failed chan struct{}
			switch desired.Mode {
			case runnerlifecycle.Running:
				armed, failure, failed = &s.failInitial, s.initialFailure, s.failed
			case runnerlifecycle.Closing:
				armed, failure, failed = &s.failTeardown, s.teardownFailure, s.teardownFailed
			}
			if armed != nil {
				_, getErr := s.State.Get(ctx, value.Metadata())
				if state.IsNotFoundError(getErr) && armed.CompareAndSwap(true, false) {
					close(failed)
					return failure
				}
			}
		}
	}

	return s.State.Modify(ctx, value, update, opts...)
}

func failedInitialOperationHost(t *testing.T, failTeardown bool) (*Host, *initialOperationFailureState, string) {
	t.Helper()
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	marker := filepath.Join(t.TempDir(), "harness-launched")
	binary := filepath.Join(t.TempDir(), "harness")
	script := fmt.Sprintf("#!/bin/sh\ntouch %s\nexec sleep 1000\n", shellQuote(marker))
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	initialFailure := errors.New("injected initial operation publication failure")
	teardownFailure := errors.New("injected teardown operation publication failure")
	store := &initialOperationFailureState{
		State:           state.WrapCore(namespaced.NewState(inmem.Build)),
		initialFailure:  initialFailure,
		teardownFailure: teardownFailure,
		failed:          make(chan struct{}),
		teardownFailed:  make(chan struct{}),
	}
	store.failInitial.Store(true)
	store.failTeardown.Store(failTeardown)
	r, err := newTestHost(lifecycleConfig(t, proxy.URL, workspace, nil, binary), store)
	if err != nil {
		t.Fatal(err)
	}
	if err := bindHost(r, "test-incarnation"); err != nil {
		t.Fatal(err)
	}
	if err := r.sessions.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		r.cancel()
		<-r.lifecycle.done.Done()
		r.lifecycle.workers.Wait()
		if r.sessions != nil {
			_ = r.sessions.Close()
		}
		_ = r.owner.Close()
	})

	if _, err := r.OpenSession(hostContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SubmitInput(hostContext(r, context.Background()), &environmentruntimepb.SubmitInputRequest{SessionId: "session-1", CommandId: "input-1", Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case <-store.failed:
	case <-ctx.Done():
		t.Fatal("initial operation publication was not attempted")
	}
	select {
	case err := <-r.fatal:
		if !strings.Contains(err.Error(), initialFailure.Error()) {
			t.Fatalf("Host failure = %v, want publication failure", err)
		}
	case <-ctx.Done():
		t.Fatal("initial operation publication failure was not reported")
	}
	return r, store, marker
}

func TestCloseSettlesAfterInitialOperationPublicationFailure(t *testing.T) {
	r, _, marker := failedInitialOperationHost(t, false)

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	closeCtx, cancelClose := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelClose()
	select {
	case err := <-closeDone:
		if err != nil {
			t.Fatalf("Close after initial publication failure: %v", err)
		}
	case <-closeCtx.Done():
		r.cancel()
		select {
		case <-closeDone:
		case <-time.After(time.Second):
			t.Fatal("Close did not return after Host cancellation")
		}
		t.Fatal("Close hung after initial operation publication failure")
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("failed initial operation launched the native harness: stat err=%v", err)
	}
}

func TestCloseReportsTeardownOperationPublicationFailureAndRetainsOwner(t *testing.T) {
	r, store, marker := failedInitialOperationHost(t, true)
	closeCtx, cancelClose := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelClose()
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	select {
	case err := <-closeDone:
		if err == nil || !strings.Contains(err.Error(), "publish operation start") {
			t.Fatalf("Close error after teardown publication failure = %v, want publication error", err)
		}
		select {
		case <-store.teardownFailed:
		default:
			t.Fatal("Close returned before the teardown publication fault was injected")
		}
	case <-closeCtx.Done():
		r.cancel()
		select {
		case <-closeDone:
		case <-time.After(time.Second):
			t.Fatal("Close did not return after Host cancellation")
		}
		t.Fatal("Close hung after teardown operation publication failure")
	}
	if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("failed initial operation launched the native harness: stat err=%v", err)
	}
	otherOwner, err := retained.OpenOwner(filepath.Join(r.config.StateDir, "harness-host"))
	if err == nil {
		_ = otherOwner.Close()
		t.Fatal("Host released the state owner although shutdown cleanup failed")
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
	r := newLifecycleHost(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	openStarted = true
	go func() {
		_, err := r.OpenSession(hostContext(r, context.Background()), openLifecycleSession("late-session", filepath.Join(workspace, "project")))
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
		_, err := quiesceHost(quiesceCtx, r)
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
	if err := <-openDone; status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("late session creation error = %v, want Aborted", err)
	}
	openFinished = true
	if exists, err := r.store.Exists("late-session"); err != nil || exists {
		t.Fatalf("late session was persisted: exists=%t err=%v", exists, err)
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
		Launch:        configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}, Bootstrap: bootstrap},
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

func newLifecycleHost(t *testing.T, config Config) *Host {
	t.Helper()
	return newLifecycleHostWithState(t, config, nil)
}

func newLifecycleHostWithState(t *testing.T, config Config, store state.State) *Host {
	t.Helper()
	r, err := newTestHost(config, store)
	if err != nil {
		t.Fatal(err)
	}
	if err := bindHost(r, "test-incarnation"); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	if err := r.sessions.Startup(context.Background()); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(context.Background()); err != nil {
			t.Errorf("close test host: %v", err)
		}
	})
	return r
}

func openLifecycleSession(sessionID, cwd string) *environmentruntimepb.OpenSessionRequest {
	return &environmentruntimepb.OpenSessionRequest{
		SessionId: sessionID,
		Spec:      &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model"},
	}
}

func TestScriptOutputFailureStopsProcessAndReturnsCause(t *testing.T) {
	r := newBareRunner(t)
	failure := fmt.Errorf("bootstrap output storage failed")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := r.scripts.Run(ctx, "test-script-output-failure", "printf output; exec sleep 1000", t.TempDir(), func(bool, []byte) error { return failure })
	if !errors.Is(err, failure) {
		t.Fatalf("output failure was lost: %v", err)
	}
	if ctx.Err() != nil {
		t.Fatalf("script waited for timeout after output failed: %v", err)
	}
}

func TestScriptRunPreservesEnvironmentStreamsAndExitStatus(t *testing.T) {
	t.Setenv("SCRIPT_RUNNER_INHERITED", "before")
	t.Setenv("SCRIPT_RUNNER_CONFIG", "ambient")
	r, err := newTestHost(Config{
		StateDir:           t.TempDir(),
		HarnessEnvironment: map[string]string{"SCRIPT_RUNNER_CONFIG": "configured"},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(context.Background()); err != nil {
			t.Errorf("close test host: %v", err)
		}
	})
	t.Setenv("SCRIPT_RUNNER_INHERITED", "after")

	var outputMu sync.Mutex
	var stdout, stderr strings.Builder
	script := `printf '%s|%s' "$SCRIPT_RUNNER_CONFIG" "$SCRIPT_RUNNER_INHERITED"; printf 'diagnostic' >&2; exit 23`
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	code, err := r.scripts.Run(ctx, "test-script", script, t.TempDir(), func(isStdout bool, data []byte) error {
		outputMu.Lock()
		defer outputMu.Unlock()
		if isStdout {
			_, err := stdout.Write(data)
			return err
		}
		_, err := stderr.Write(data)
		return err
	})
	if err != nil {
		t.Fatalf("run script: %v", err)
	}
	if code != 23 {
		t.Fatalf("script exit code = %d, want 23", code)
	}
	outputMu.Lock()
	gotStdout, gotStderr := stdout.String(), stderr.String()
	outputMu.Unlock()
	if gotStdout != "configured|after" || gotStderr != "diagnostic" {
		t.Fatalf("script output = stdout %q, stderr %q; want stdout %q, stderr %q", gotStdout, gotStderr, "configured|after", "diagnostic")
	}
}
