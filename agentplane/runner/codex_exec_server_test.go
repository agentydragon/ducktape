package runner

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/configuration"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	executorcontrol "github.com/agentydragon/ducktape/agentplane/runner/executor/control"
	"github.com/agentydragon/ducktape/agentplane/runner/executorrelay"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"github.com/coder/websocket"
	"github.com/cosi-project/runtime/pkg/resource"
)

const codexExecServerTestTimeout = 10 * time.Second

func TestClosingExecutorStreamPreservesSiblingInSameEpoch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			return
		}
		defer connection.CloseNow()
		for {
			kind, data, err := connection.Read(request.Context())
			if err != nil {
				return
			}
			if err := connection.Write(request.Context(), kind, data); err != nil {
				return
			}
		}
	}))
	defer server.Close()
	r := newExecutorLifecycleRunnerWithURL(t, "ws"+strings.TrimPrefix(server.URL, "http"), func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	ctx, cancel := context.WithTimeout(t.Context(), codexExecServerTestTimeout)
	defer cancel()
	frames := []chan *executorpb.ExecutorFrame{make(chan *executorpb.ExecutorFrame, 8), make(chan *executorpb.ExecutorFrame, 8)}
	connections := make([]*executorrelay.Connection, 0, 2)
	for i, sessionID := range []string{"session-1", "session-2"} {
		send := func(frame *executorpb.ExecutorFrame) error {
			select {
			case frames[i] <- frame:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		connection, err := r.executorRelay.Open(ctx, 7, send, send)
		if err != nil {
			t.Fatalf("open executor connection: %v", err)
		}
		defer connection.Close()
		connections = append(connections, connection)
		if err := connection.Route(&executorpb.ExecutorFrame{
			Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: sessionID}},
		}); err != nil {
			t.Fatal(err)
		}
		select {
		case ready := <-frames[i]:
			if !ready.GetReady().GetAccepted() {
				t.Fatalf("executor %s readiness = %v", sessionID, ready)
			}
		case <-ctx.Done():
			t.Fatal("executor did not become ready")
		}
	}
	connections[0].Close()
	want := []byte{0, 1, 2, 255}
	if err := connections[1].Route(&executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{
			Opcode: executorpb.ExecutorMessage_OPCODE_BINARY, Payload: want,
		}},
	}); err != nil {
		t.Fatalf("sibling stream closed: %v", err)
	}
	select {
	case reply := <-frames[1]:
		if message := reply.GetMessage(); message == nil || message.GetOpcode() != executorpb.ExecutorMessage_OPCODE_BINARY || string(message.GetPayload()) != string(want) {
			t.Fatalf("sibling executor reply = %v, want binary echo %v", reply, want)
		}
	case <-ctx.Done():
		t.Fatal("sibling executor stopped exchanging data after another stream closed")
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

func TestExecutorRelayReadinessReturnsTypedCompanionRefusal(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		return nil, codexexecserver.Refusal{Reason: "Codex binary is unavailable"}
	}, nil)
	if err := requestExecutor(r); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	_, err := r.executorRelay.WaitReady(ctx)
	var refusal codexexecserver.Refusal
	if !errors.As(err, &refusal) || refusal.Reason != "Codex binary is unavailable" {
		t.Fatalf("relay readiness = %v (%T), want the typed companion refusal", err, err)
	}
}

func TestExecutorRelayReadinessSurvivesTransientStartupFailure(t *testing.T) {
	var starts atomic.Int32
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		if starts.Add(1) == 1 {
			return nil, errors.New("transient native executor launch failure")
		}
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	if err := requestExecutor(r); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	last, err := r.executorRelay.WaitReady(ctx)
	if err != nil {
		t.Fatalf("relay readiness after controller retry: %v", err)
	}
	if starts.Load() < 2 || last.Runtime.Phase != codexexecserver.Ready {
		t.Fatalf("startup attempts/runtime = %d/%+v, want a successful retry", starts.Load(), last.Runtime)
	}
}

func TestExecutorRelayGatewayCloseCancelsBlockedReadiness(t *testing.T) {
	started := make(chan struct{})
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*supervision.Instance, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}, nil)
	frames := make(chan *executorpb.ExecutorFrame, 2)
	send := func(frame *executorpb.ExecutorFrame) error {
		frames <- frame
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	connection, err := r.executorRelay.Open(ctx, 7, send, send)
	if err != nil {
		t.Fatalf("open executor connection: %v", err)
	}
	defer connection.Close()
	open := &executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: "session-1"}},
	}
	if err := connection.Route(open); err != nil {
		t.Fatalf("open executor relay: %v", err)
	}
	select {
	case <-started:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor start did not block as expected")
	}
	closeFrame := &executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Close{Close: &executorpb.ExecutorClose{Code: 1001}},
	}
	if err := connection.Route(closeFrame); err != nil {
		t.Fatalf("close executor relay: %v", err)
	}
	joined := make(chan struct{})
	go func() {
		connection.Close()
		close(joined)
	}()
	select {
	case <-joined:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("Gateway close did not join the readiness wait")
	}
	if r.executorRelay.Active() {
		t.Fatal("executor relay remained active after Gateway close")
	}
	select {
	case frame := <-frames:
		if frame.GetReady() == nil || frame.GetReady().GetAccepted() {
			t.Fatalf("relay readiness response = %v, want rejected readiness after close", frame)
		}
	case <-ctx.Done():
		t.Fatal("relay did not report readiness rejection")
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
		r := newCodexSynctestRunner(t, stateDir, func(context.Context) (*supervision.Instance, error) {
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
		_, readinessErr := r.executorRelay.WaitReady(readinessCtx)
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

func TestExecutorRelayAdmissionClosesWhenRunnerQuiesces(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, func(context.Context) error { return nil })
	if err := r.store.AdoptIncarnation("test-incarnation"); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("quiesce Runner: %v", err)
	}

	frames := make(chan *executorpb.ExecutorFrame, 1)
	send := func(frame *executorpb.ExecutorFrame) error {
		frames <- frame
		return nil
	}
	connection, err := r.executorRelay.Open(context.Background(), 1, send, send)
	if err != nil {
		t.Fatalf("open executor stream: %v", err)
	}
	defer connection.Close()
	if err := connection.Route(&executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: "session-1"}},
	}); err != nil {
		t.Fatalf("route executor open after quiesce: %v", err)
	}
	select {
	case frame := <-frames:
		if frame.GetReady() == nil || frame.GetReady().GetAccepted() {
			t.Fatalf("quiescing Runner executor readiness = %v, want rejected", frame)
		}
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("quiescing Runner did not reject executor relay")
	}
	if r.executorRelay.Active() {
		t.Fatal("quiescing Runner admitted an executor relay")
	}
}

func TestExecutorShutdownDrainsRelaysBeforeFencing(t *testing.T) {
	var stops atomic.Int32
	var drained atomic.Bool
	var r *Runner
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			return
		}
		defer connection.CloseNow()
		for {
			if _, _, err := connection.Read(request.Context()); err != nil {
				return
			}
		}
	}))
	defer server.Close()
	r = newExecutorLifecycleRunnerWithURL(t, "ws"+strings.TrimPrefix(server.URL, "http"), func(context.Context) (*supervision.Instance, error) {
		done := make(chan error, 1)
		return &supervision.Instance{
			Done: done,
			Stop: func(context.Context) error {
				if r.executorRelay.Active() {
					return errors.New("executor stopped before relay drained")
				}
				drained.Store(true)
				stops.Add(1)
				done <- nil
				return nil
			},
		}, nil
	}, func(context.Context) error { return nil })
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	if err := ensureExecutor(ctx, r); err != nil {
		t.Fatalf("start executor: %v", err)
	}

	frames := make(chan *executorpb.ExecutorFrame, 1)
	send := func(frame *executorpb.ExecutorFrame) error {
		frames <- frame
		return nil
	}
	connection, err := r.executorRelay.Open(ctx, 7, send, send)
	if err != nil {
		t.Fatalf("open executor stream: %v", err)
	}
	defer connection.Close()
	if err := connection.Route(&executorpb.ExecutorFrame{
		Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{SessionId: "session-1"}},
	}); err != nil {
		t.Fatalf("open executor relay: %v", err)
	}
	select {
	case frame := <-frames:
		if frame.GetReady() == nil || !frame.GetReady().GetAccepted() {
			t.Fatalf("executor readiness = %v, want accepted", frame)
		}
	case <-ctx.Done():
		t.Fatal("executor relay did not become ready")
	}
	if !r.executorRelay.Active() {
		t.Fatal("executor relay is not active before Runner close")
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
		Codex:    &harness.LaunchConfig{Binary: "/bin/false"},
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
