package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type failingCloseHostLifetime struct {
	hostLifetime
	stops atomic.Int32
}

type uncertainCloseExecution struct{ fences atomic.Int32 }

func (*uncertainCloseExecution) Executor() executorpb.ExecutorServer { return nil }
func (e *uncertainCloseExecution) StopAndFence(context.Context) error {
	e.fences.Add(1)
	return errors.New("uncertain Execution backend fence")
}

func TestRuntimeCloseUncertainExecutionFenceRetainsOwner(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRunner(t, config)
	execution := &uncertainCloseExecution{}
	r.mu.Lock()
	r.executionOwner = execution
	r.mu.Unlock()

	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "uncertain Execution backend fence") {
			t.Fatalf("Close = %v, want uncertain fence error", err)
		}
	}
	if got := execution.fences.Load(); got != 1 {
		t.Fatalf("Execution fence attempts = %d, want one", got)
	}
	status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || status.Stage != lifecycle.RuntimeCloseFailed {
		t.Fatalf("Close aggregate = %+v, found=%t, err=%v", status, found, err)
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("uncertain Execution fence released environment owner")
	}
}

func (h *failingCloseHostLifetime) Stop(ctx context.Context) error {
	h.stops.Add(1)
	return errors.Join(h.hostLifetime.Stop(ctx), errors.New("injected Host join failure"))
}

func TestRuntimeCloseFailureRetainsOwnerAndDoesNotRetryHostStop(t *testing.T) {
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	r := newLifecycleRunner(t, config)
	host := &failingCloseHostLifetime{hostLifetime: r.hostLifetime}
	r.mu.Lock()
	r.hostLifetime = host
	r.mu.Unlock()

	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		err := r.Close(ctx)
		cancel()
		if err == nil || !strings.Contains(err.Error(), "injected Host join failure") {
			t.Fatalf("Close = %v, want exact Host join failure", err)
		}
	}
	if got := host.stops.Load(); got != 1 {
		t.Fatalf("Host stop calls = %d, want one", got)
	}
	status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || status.Stage != lifecycle.RuntimeCloseFailed || !strings.Contains(status.Error, "injected Host join failure") {
		t.Fatalf("Close aggregate = %+v, found=%t, err=%v", status, found, err)
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("failed Host join released environment owner")
	}
}

func TestFreshSessionAdmissionInvalidatesIdleToken(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	before, err := r.facts()
	if err != nil || before.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", before, err)
	}
	if _, err := client.runner.OpenSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{IfIdleSince: before.IdleSince}); status.Code(err) != codes.Aborted {
		t.Fatalf("stale idle conditional=%v", err)
	}
	waitRuntime(t, r, func() bool { facts, err := r.facts(); return err == nil && facts.IdleSince != nil }, "admitted Session idle")
	after, err := r.facts()
	if err != nil || after.GetIdleSince() == nil || proto.Equal(before.IdleSince, after.IdleSince) {
		t.Fatalf("admission did not invalidate idle token: before=%v after=%v error=%v", before, after, err)
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{IfIdleSince: after.IdleSince}); err != nil {
		t.Fatal(err)
	}
}

func TestEmbeddedHostLifecycleLossStillFencesExecution(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	config := executionRuntimeConfig(t, url, nil)
	config.expectCloseError = true
	started := make(chan *localExecutionHandle, 1)
	startExecution := config.deps.startExecution
	config.deps.startExecution = func(ctx context.Context, cfg Config, uid string) (executionHandle, error) {
		handle, err := startExecution(ctx, cfg, uid)
		if err == nil {
			started <- handle.(*localExecutionHandle)
		}
		return handle, err
	}
	r := newLifecycleRunner(t, config)
	var execution *localExecutionHandle
	select {
	case execution = <-started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Execution service did not start")
	}
	r.mu.Lock()
	host := r.host
	r.mu.Unlock()
	r.disconnectHost(host, errors.New("simulated Host lifecycle loss"))
	closeCtx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(closeCtx); err == nil {
		t.Fatal("Runtime silently accepted lost Host lifecycle")
	}
	if !execution.stopRequested() {
		t.Fatal("Runtime skipped Execution fence after Host lifecycle loss")
	}
}

func TestRepeatedQuiesceKeepsCurrentLifecycleGeneration(t *testing.T) {
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	facts, err := r.facts()
	if err != nil || facts.GetIdleSince() == nil {
		t.Fatalf("initial idle facts=%v, %v", facts, err)
	}
	request := &sessionpb.QuiesceRequest{IfIdleSince: facts.IdleSince}
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.intent != nil && r.host.report != nil && r.host.report.Ready && proto.Equal(r.host.report.Binding, r.intent.Binding)
	}, "current quiesce lifecycle report")
	r.mu.Lock()
	desired := r.desired
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	if _, err := r.Quiesce(client.ctx, request); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.desired != desired || !proto.Equal(r.intent.Binding, binding) {
		t.Fatalf("repeated Quiesce changed lifecycle generation: desired=%+v want=%+v, binding=%v want=%v", r.desired, desired, r.intent.Binding, binding)
	}
}

func TestBootstrapAndSetupCompleteOnCloseAndDoNotRerun(t *testing.T) {
	root := t.TempDir()
	workspace := t.TempDir()
	bootstrapStarted := filepath.Join(root, "bootstrap-started")
	bootstrapRelease := filepath.Join(root, "bootstrap-release")
	bootstrapCount := filepath.Join(root, "bootstrap-count")
	setupStarted := filepath.Join(root, "setup-started")
	setupRelease := filepath.Join(root, "setup-release")
	setupCount := filepath.Join(root, "setup-count")
	bootstrapScript := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf b >> %s", shellQuote(bootstrapStarted), shellQuote(bootstrapRelease), shellQuote(bootstrapCount))
	setup := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf s >> %s", shellQuote(setupStarted), shellQuote(setupRelease), shellQuote(setupCount))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &bootstrapScript, "/bin/false")
	config.StateDir = root
	r := newLifecycleRunner(t, config)
	t.Cleanup(func() { _ = os.WriteFile(bootstrapRelease, nil, 0600); _ = os.WriteFile(setupRelease, nil, 0600) })
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	request.SetupScript = &setup
	if _, err := client.runner.OpenSession(client.callCtx, request); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, bootstrapStarted)
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Runtime close intent")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned during bootstrap: %v", err)
	default:
	}
	if err := os.WriteFile(bootstrapRelease, nil, 0600); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, setupStarted)
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned during accepted setup: %v", err)
	default:
	}
	if err := os.WriteFile(setupRelease, nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("effect %s=%q, %v", path, data, err)
		}
	}
	restarted := newLifecycleRunner(t, config)
	successor := startDirectExecutionTestClient(t, restarted, "test-incarnation")
	if _, err := successor.runner.OpenSession(successor.callCtx, request); err != nil {
		t.Fatal(err)
	}
	sessions, err := successor.runner.ListSessions(successor.callCtx, &sessionpb.ListSessionsRequest{})
	if err != nil || len(sessions.GetSessions()) != 1 || sessions.Sessions[0].SetupState != sessionpb.SetupState_SETUP_STATE_SUCCEEDED {
		t.Fatalf("recovered setup=%v, %v", sessions, err)
	}
	waitRuntime(t, restarted, func() bool {
		facts, err := restarted.facts()
		return err == nil && facts.Bootstrap == environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED
	}, "durable bootstrap result")
	for path, want := range map[string]string{bootstrapCount: "b", setupCount: "s"} {
		data, err := os.ReadFile(path)
		if err != nil || string(data) != want {
			t.Fatalf("recovery replayed effect %s=%q, %v", path, data, err)
		}
	}
}
