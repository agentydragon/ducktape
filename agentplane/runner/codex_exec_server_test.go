package runner

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/configuration"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	executorcontrol "github.com/agentydragon/ducktape/agentplane/runner/executor/control"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/resource"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const codexExecServerTestTimeout = 10 * time.Second

func TestClosingExecutorStreamPreservesSiblingInSameEpoch(t *testing.T) {
	url, opened, closed := startNativeExecutionTestServer(t, true)
	r := newExecutorLifecycleRunnerWithURL(t, url, func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")

	firstCtx, cancelFirst := context.WithCancel(client.callCtx)
	first, err := client.open(firstCtx, "sampler-session-1", "open-native-1")
	if err != nil {
		t.Fatalf("open first execution session: %v", err)
	}
	firstResponse, err := first.Recv()
	if err != nil || firstResponse.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("first OpenSession response = %v, err=%v", firstResponse, err)
	}
	second, err := client.open(client.callCtx, "sampler-session-2", "open-native-2")
	if err != nil {
		t.Fatalf("open sibling execution session: %v", err)
	}
	secondResponse, err := second.Recv()
	if err != nil || secondResponse.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("sibling OpenSession response = %v, err=%v", secondResponse, err)
	}
	if firstResponse.GetExecutionSessionId() == secondResponse.GetExecutionSessionId() {
		t.Fatalf("Runner reused execution session ID %q", firstResponse.GetExecutionSessionId())
	}
	for range 2 {
		select {
		case <-opened:
		case <-time.After(codexExecServerTestTimeout):
			t.Fatal("local native sessions did not initialize")
		}
	}

	cancelFirst()
	select {
	case closedID := <-closed:
		if closedID != firstResponse.GetExecutionSessionId() {
			t.Fatalf("closed native session %q, want first session %q", closedID, firstResponse.GetExecutionSessionId())
		}
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("closing one OpenSession did not close its local WebSocket")
	}
	select {
	case native := <-closed:
		t.Fatalf("closing one OpenSession also closed sibling native session %q", native)
	default:
	}

	evidence, err := second.Recv()
	if err != nil {
		t.Fatalf("sibling OpenSession stopped after another stream closed: %v", err)
	}
	if len(evidence.GetNativeFrames()) != 1 || !strings.Contains(string(evidence.GetNativeFrames()[0].GetPayload()), secondResponse.GetExecutionSessionId()) {
		t.Fatalf("sibling native notification = %v, want its own session ID %q", evidence.GetNativeFrames(), secondResponse.GetExecutionSessionId())
	}
}

func TestExecutorEnsureReturnsTypedCompanionRefusal(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		return nil, codexexecserver.Refusal{Reason: "Codex binary is unavailable"}
	}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	err := ensureExecutor(ctx, r)
	var refusal codexexecserver.Refusal
	if !errors.As(err, &refusal) || refusal.Reason != "Codex binary is unavailable" {
		t.Fatalf("ensure = %v (%T), want the typed companion refusal", err, err)
	}
}

func TestExecutionSessionReadinessReturnsTypedCompanionRefusal(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		return nil, codexexecserver.Refusal{Reason: "Codex binary is unavailable"}
	}, nil)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	stream, err := client.open(client.callCtx, "sampler-session", "open-refusal")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil {
		t.Fatalf("OpenSession rejection response: %v", err)
	}
	if response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_REJECTED || !strings.Contains(response.GetStatus().GetMessage(), "Codex binary is unavailable") {
		t.Fatalf("OpenSession outcome = %v, want typed rejection with companion reason", response.GetStatus())
	}
	if response.GetStatus().UpstreamCode != nil {
		t.Fatalf("companion refusal fabricated a native upstream code: %v", response.GetStatus())
	}
}

func TestExecutionSessionReadinessSurvivesTransientStartupFailure(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	var starts atomic.Int32
	r := newExecutorLifecycleRunnerWithURL(t, url, func(context.Context) (*supervision.Instance, error) {
		if starts.Add(1) == 1 {
			return nil, errors.New("transient native executor launch failure")
		}
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	stream, err := client.open(client.callCtx, "sampler-session", "open-after-retry")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("OpenSession after controller retry = %v, err=%v", response, err)
	}
	if starts.Load() < 2 || response.GetExecutionSessionId() == "" {
		t.Fatalf("startup attempts/session ID = %d/%q, want successful retry", starts.Load(), response.GetExecutionSessionId())
	}
}

func TestExecutionSessionCancellationStopsBlockedReadinessWait(t *testing.T) {
	started := make(chan struct{})
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*supervision.Instance, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}, nil)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	callCtx, cancel := context.WithCancel(client.callCtx)
	stream, err := client.open(callCtx, "sampler-session", "open-blocked")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	select {
	case <-started:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor startup did not block as expected")
	}
	cancel()
	if _, err := stream.Recv(); err == nil {
		t.Fatal("canceled OpenSession returned a response")
	}
	deadline := time.Now().Add(codexExecServerTestTimeout)
	for r.execution.Active() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if r.execution.Active() {
		t.Fatal("execution admission remained active after the caller canceled")
	}
}

func TestReopeningRunnerAdmissionClearsExecServerDemand(t *testing.T) {
	var starts atomic.Int32
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		starts.Add(1)
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, func(context.Context) error { return nil })

	r.mu.Lock()
	for range 4 {
		if err := r.codexExecServer.RequestLocked(r.ctx); err != nil {
			r.mu.Unlock()
			t.Fatal(err)
		}
	}
	before, found, err := codexexecserver.ReadInput(r.ctx, r.lifecycle.state)
	if err != nil || !found || before.Demand != 4 {
		r.mu.Unlock()
		t.Fatalf("executor demand before reopening = %+v, found=%t err=%v", before, found, err)
	}
	if err := r.lifecycle.setDesiredState(runnerlifecycle.Quiescing, "test-incarnation"); err != nil {
		r.mu.Unlock()
		t.Fatal(err)
	}
	if err := r.lifecycle.setDesiredState(runnerlifecycle.Running, ""); err != nil {
		r.mu.Unlock()
		t.Fatal(err)
	}
	r.mu.Unlock()

	after, found, err := codexexecserver.ReadInput(r.ctx, r.lifecycle.state)
	if err != nil || !found || after.Demand != 0 {
		t.Fatalf("executor demand after reopening = %+v, found=%t err=%v", after, found, err)
	}
	if starts.Load() != 0 {
		t.Fatalf("stale executor demand started %d native processes", starts.Load())
	}
}

func TestExecutorConcurrentStartupPublishesOneProcessAndClosesThroughCOSI(t *testing.T) {
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*supervision.Instance, error) {
		starts.Add(1)
		entered <- struct{}{}
		select {
		case <-release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return newTestExecutorInstance(&stops), nil
	}, func(context.Context) error { return nil })

	results := make(chan error, 2)
	for range 2 {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
			defer cancel()
			results <- ensureExecutor(ctx, r)
		}()
	}
	select {
	case <-entered:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor start operation did not begin")
	}
	close(release)
	for range 2 {
		select {
		case err := <-results:
			if err != nil {
				t.Fatalf("concurrent ensure: %v", err)
			}
		case <-time.After(codexExecServerTestTimeout):
			t.Fatal("concurrent ensure did not observe readiness")
		}
	}
	if got := starts.Load(); got != 1 {
		t.Fatalf("started %d executor instances, want one", got)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatalf("close through COSI: %v", err)
	}
	if got := stops.Load(); got != 1 {
		t.Fatalf("fenced %d executor instances, want one", got)
	}
	if current := executorRuntimeState(r); current.Phase != codexexecserver.Stopped || current.StopError != "" {
		t.Fatalf("executor runtime after close = %+v, want a clean stopped state", current)
	}
}

func TestExecutorFailedFenceRetainsOwnershipAndBlocksSuccessor(t *testing.T) {
	stateDir := t.TempDir()
	synctest.Test(t, func(t *testing.T) {
		var starts, stops atomic.Int32
		fenceFailure := errors.New("executor container could not prove fencing")
		done := make(chan error, 1)
		instance := &supervision.Instance{
			Done: done,
			Stop: func(context.Context) error {
				stops.Add(1)
				return fenceFailure
			},
		}
		r := newCodexDeadlineRunner(t, stateDir, time.Second, func(context.Context) (*supervision.Instance, error) {
			if starts.Add(1) == 1 {
				return instance, nil
			}
			return newTestExecutorInstance(&stops), nil
		})

		if err := requestExecutor(r); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		ready := executorRuntimeState(r)
		if ready.Phase != codexexecserver.Ready {
			t.Fatalf("executor before native exit = %+v, want ready", ready)
		}
		done <- errors.New("executor exited unexpectedly")
		synctest.Wait()

		failed := executorRuntimeState(r)
		if !strings.Contains(failed.StopError, fenceFailure.Error()) {
			t.Fatalf("executor StopError = %q, want %q", failed.StopError, fenceFailure)
		}
		if failed.Phase != codexexecserver.Stopped || failed.Generation != ready.Generation {
			t.Fatalf("executor runtime after failed fence = %+v, want stopped generation %d", failed, ready.Generation)
		}
		desired, err := r.lifecycle.state.Get(r.ctx, resource.NewMetadata(runnerlifecycle.Namespace, supervision.DesiredStateType, failed.ProcessID, resource.VersionUndefined))
		if err != nil {
			t.Fatal(err)
		}
		if !desired.Metadata().Finalizers().Has(supervision.ControllerName) {
			t.Fatal("failed executor fence released the process finalizer")
		}
		process, found, err := supervision.ReadStatus(r.ctx, r.lifecycle.state, runnerlifecycle.Namespace, failed.ProcessID)
		if err != nil || !found || process.StopError == "" {
			t.Fatalf("process after failed fence = %+v, found=%v err=%v", process, found, err)
		}
		if got := starts.Load(); got != 1 || stops.Load() != 1 {
			t.Fatalf("executor starts/stops after failed fence = %d/%d, want 1/1", got, stops.Load())
		}

		if err := requestExecutor(r); err != nil {
			t.Fatalf("publish additional executor demand after failed fence: %v", err)
		}
		synctest.Wait()
		blocked := executorRuntimeState(r)
		if blocked.Phase != codexexecserver.Stopped || blocked.Generation != ready.Generation || blocked.StopError == "" || starts.Load() != 1 {
			t.Fatalf("executor advanced after failed fence and new demand: runtime=%+v starts=%d", blocked, starts.Load())
		}
		readinessCtx, cancelReadiness := context.WithTimeout(context.Background(), time.Second)
		defer cancelReadiness()
		_, readinessErr := r.acquireExecution(readinessCtx)
		if readinessErr == nil || !strings.Contains(readinessErr.Error(), fenceFailure.Error()) {
			t.Fatalf("executor readiness after failed fence = %v, want terminal fence failure", readinessErr)
		}

		closeErr := r.Close(context.Background())
		if closeErr == nil || !strings.Contains(closeErr.Error(), fenceFailure.Error()) {
			t.Fatalf("Runner Close after failed executor fence = %v, want the fence failure", closeErr)
		}
	})
}

func TestExecutorCloseCancelsConcurrentStartupWithoutRestart(t *testing.T) {
	entered := make(chan struct{}, 1)
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*supervision.Instance, error) {
		starts.Add(1)
		entered <- struct{}{}
		<-ctx.Done()
		return nil, ctx.Err()
	}, nil)

	ensured := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
		defer cancel()
		ensured <- ensureExecutor(ctx, r)
	}()
	select {
	case <-entered:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor startup did not begin")
	}
	closed := make(chan error, 1)
	go func() { closed <- r.Close(context.Background()) }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("close during startup: %v", err)
		}
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("close did not cancel and join executor startup")
	}
	if err := <-ensured; err == nil {
		t.Fatal("ensure returned ready after close canceled startup")
	}
	if starts.Load() != 1 || stops.Load() != 0 {
		t.Fatalf("startup/stop counts = %d/%d, want one canceled start and no instance to fence", starts.Load(), stops.Load())
	}
	if current := executorRuntimeState(r); current.Phase != codexexecserver.Stopped || current.StopError != "" {
		t.Fatalf("executor runtime after close = %+v, want clean stopped state", current)
	}
}

func TestExecutionSessionAdmissionClosesWhenRunnerQuiesces(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, func(context.Context) error { return nil })
	if err := r.store.AdoptIncarnation("test-incarnation"); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("quiesce Runner: %v", err)
	}
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	stream, err := client.open(client.callCtx, "sampler-session", "open-after-quiesce")
	if err == nil {
		_, err = stream.Recv()
	}
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("OpenSession after quiesce = %v, want FailedPrecondition", err)
	}
	if r.execution.Active() {
		t.Fatal("quiescing Runner admitted an execution session")
	}
}

func TestExecutorShutdownDrainsSessionsBeforeFencing(t *testing.T) {
	url, opened, closed := startNativeExecutionTestServer(t, false)
	var stops atomic.Int32
	var drained atomic.Bool
	var r *Runner
	r = newExecutorLifecycleRunnerWithURL(t, url, func(context.Context) (*supervision.Instance, error) {
		done := make(chan error, 1)
		return &supervision.Instance{
			Done: done,
			Stop: func(context.Context) error {
				if r.execution.Active() {
					return errors.New("executor stopped before execution sessions drained")
				}
				select {
				case <-closed:
				default:
					return errors.New("executor stopped before the local WebSocket closed")
				}
				drained.Store(true)
				stops.Add(1)
				done <- nil
				return nil
			},
		}, nil
	}, func(context.Context) error { return nil })
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	stream, err := client.open(client.callCtx, "sampler-session", "open-for-shutdown")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("OpenSession before shutdown = %v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("local execution session did not initialize")
	}
	if !r.execution.Active() {
		t.Fatal("execution session is not active before Runner close")
	}
	if err := closeRunnerForTest(t, r); err != nil {
		t.Fatalf("close Runner: %v", err)
	}
	if !drained.Load() || stops.Load() != 1 {
		t.Fatalf("executor shutdown observed drained=%t stops=%d, want true/1", drained.Load(), stops.Load())
	}
}

func newExecutorLifecycleRunner(t *testing.T, start func(context.Context) (*supervision.Instance, error), probe func(context.Context) error) *Runner {
	t.Helper()
	return newExecutorLifecycleRunnerWithURL(t, "ws://127.0.0.1:4599", start, probe)
}

func newExecutorLifecycleRunnerWithURL(t *testing.T, url string, start func(context.Context) (*supervision.Instance, error), probe func(context.Context) error) *Runner {
	return newExecutorLifecycleRunnerWithURLAndCodexBinary(t, url, "/bin/false", start, probe)
}

func newExecutorLifecycleRunnerWithURLAndCodexBinary(t *testing.T, url, codexBinary string, start func(context.Context) (*supervision.Instance, error), probe func(context.Context) error) *Runner {
	t.Helper()
	dir := t.TempDir()
	socket := filepath.Join(dir, "executor-control.sock")
	companionCtx, cancelCompanion := context.WithCancel(context.Background())
	companionDone := make(chan error, 1)
	go func() { companionDone <- serveExecutorControlStub(companionCtx, socket) }()
	waitForSocket(t, socket)

	config := Config{
		StateDir: t.TempDir(),
		Control:  insecureControl(),
		Launch:   configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}},
		Codex: &harness.LaunchConfig{
			Binary: codexBinary, BaseURL: "https://codex.example.invalid/v1", Token: "test",
		},
		CodexExecServer: &codexexecserver.Config{
			URL: url, ContainerSocket: socket,
		},
	}
	adapters := codexexecserver.Adapters{Start: start, Probe: probe}
	if adapters.Probe == nil {
		adapters.Probe = func(context.Context) error { return nil }
	}
	r, err := newRunner(config, &adapters, nil)
	if err != nil {
		cancelCompanion()
		t.Fatalf("create lifecycle Runner: %v", err)
	}
	if err := r.Startup(context.Background()); err != nil {
		cancelCompanion()
		t.Fatalf("start lifecycle Runner: %v", err)
	}
	t.Cleanup(func() {
		if err := r.Close(context.Background()); err != nil {
			t.Errorf("close executor test Runner: %v", err)
		}
		cancelCompanion()
		select {
		case err := <-companionDone:
			if err != nil {
				t.Errorf("stop executor test companion: %v", err)
			}
		case <-time.After(codexExecServerTestTimeout):
			t.Error("executor test companion did not stop")
		}
	})
	return r
}

func newTestExecutorInstance(stops *atomic.Int32) *supervision.Instance {
	done := make(chan error, 1)
	var once sync.Once
	return &supervision.Instance{
		Done: done,
		Stop: func(context.Context) error {
			stops.Add(1)
			once.Do(func() { done <- errors.New("test executor was fenced") })
			return nil
		},
	}
}

func waitExecutor(t *testing.T, r *Runner, predicate func(codexexecserver.Runtime) bool, want string) codexexecserver.Runtime {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		current := executorRuntimeState(r)
		if predicate(current) {
			return current
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s: executor=%+v", want, current)
		}
	}
}

func serveExecutorControlStub(ctx context.Context, socket string) error {
	if err := os.MkdirAll(filepath.Dir(socket), 0o700); err != nil {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	stop := context.AfterFunc(ctx, func() { _ = listener.Close() })
	defer stop()
	status := executorcontrol.Status{Generation: "executor-test-generation"}
	for {
		connection, err := listener.Accept()
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			return err
		}
		go func(c net.Conn) {
			defer c.Close()
			var request executorcontrol.Request
			if json.NewDecoder(c).Decode(&request) == nil && request.Operation == "inspect" {
				_ = json.NewEncoder(c).Encode(status)
			}
		}(connection)
	}
}

func waitForSocket(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("no companion socket at %s", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// requestExecutor follows the app-server's production admission path: the Runner lock is held
// while the component publishes demand.
func requestExecutor(r *Runner) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.codexExecServer == nil || r.lifecycle == nil || !r.lifecycle.accepting() {
		return errors.New("runner is not accepting new Codex exec-server demand")
	}
	return r.codexExecServer.RequestLocked(r.ctx)
}

func ensureExecutor(ctx context.Context, r *Runner) error {
	if err := requestExecutor(r); err != nil {
		return err
	}
	return r.lifecycle.waitFor(ctx, func() (bool, error) {
		current := executorRuntimeState(r)
		if current.Phase == codexexecserver.Ready {
			return true, nil
		}
		if current.Refusal != "" {
			return true, codexexecserver.Refusal{Reason: current.Refusal}
		}
		if current.Phase == codexexecserver.Stopped && current.Error != "" {
			return true, errors.New(current.Error)
		}
		return false, nil
	})
}

func executorRuntimeState(r *Runner) codexexecserver.Runtime {
	current, _, err := r.codexExecServer.RuntimeState(r.ctx)
	if err != nil {
		r.fail(err)
	}
	return current
}
