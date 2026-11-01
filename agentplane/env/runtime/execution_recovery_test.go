package runtime

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
)

type recoveryTestHandle struct {
	exited    chan childExit
	fenced    chan struct{}
	fenceOnce sync.Once
	mu        sync.Mutex
	expected  bool
}

func (h *recoveryTestHandle) Executor() executorpb.ExecutorServer {
	return &executorpb.UnimplementedExecutorServer{}
}
func (h *recoveryTestHandle) StopAndFence(context.Context) error {
	h.mu.Lock()
	h.expected = true
	h.mu.Unlock()
	h.fenceOnce.Do(func() { close(h.fenced) })
	return nil
}
func (h *recoveryTestHandle) ProcessExit() <-chan childExit { return h.exited }
func (h *recoveryTestHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expected
}

func TestFencedExecutionLossRestartsAPIForSameIncarnation(t *testing.T) {
	url, opened, _ := startNativeExecutionTestServer(t, false)
	config := executionRuntimeConfig(t, url, nil)
	originalStart := config.deps.startExecution
	starts := make(chan *localExecutionHandle, 2)
	events := make(chan string, 3)
	config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
		handle, err := originalStart(ctx, config, uid)
		if err != nil {
			return nil, err
		}
		local := handle.(*localExecutionHandle)
		local.exited = make(chan childExit, 2)
		events <- "start"
		starts <- local
		return local, nil
	}
	r := newLifecycleRunner(t, config)
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	first := <-starts

	stream, err := client.open(client.callCtx, "session-before-loss", "open-before-loss")
	if err != nil {
		t.Fatalf("open before loss: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome().String() != "OUTCOME_COMPLETED" {
		t.Fatalf("initial OpenSession response=%v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("initial native executor session did not open")
	}
	if got := <-events; got != "start" {
		t.Fatalf("first lifecycle event=%q, want start", got)
	}

	events <- "fenced"
	first.loseBackendAfterFence()
	oldStreamDone := make(chan error, 1)
	go func() { _, err := stream.Recv(); oldStreamDone <- err }()
	select {
	case err := <-oldStreamDone:
		if err == nil {
			t.Fatal("lost Execution service left its old OpenSession stream active")
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("old OpenSession stream did not settle after the fenced service exit")
	}

	var replacement *localExecutionHandle
	select {
	case replacement = <-starts:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Runtime did not start a replacement Execution API service")
	}
	if replacement == first {
		t.Fatal("Runtime reused the exited Execution API process")
	}
	if got := <-events; got != "fenced" {
		t.Fatalf("replacement ordering event=%q, want fenced", got)
	}
	if got := <-events; got != "start" {
		t.Fatalf("replacement ordering event=%q, want start", got)
	}
	select {
	case session := <-opened:
		t.Fatalf("Runtime replayed the prior Session request after recovery: %q", session)
	default:
	}

	newStream, err := client.open(client.callCtx, "session-after-loss", "open-after-loss")
	if err != nil {
		t.Fatalf("open after fenced recovery: %v", err)
	}
	response, err = newStream.Recv()
	if err != nil || response.GetStatus().GetOutcome().String() != "OUTCOME_COMPLETED" {
		t.Fatalf("new OpenSession response=%v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("fresh operation did not reach the recovered Execution service")
	}
	_ = newStream.CloseSend()
}

func TestExecutionServiceUnexpectedNonzeroExitIsFatalWithoutReplacement(t *testing.T) {
	handle := &localExecutionHandle{exited: make(chan childExit, 2)}
	r := &Runtime{
		executionOwner: handle,
		executionUID:   "test-incarnation",
		intent: &runtimepb.LifecycleIntent{
			Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation",
		},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			return nil, errors.New("must not replace an unfenced process")
		}},
	}
	go r.watchExecutionProcess(handle, "test-incarnation", handle)
	handle.exitWithoutFence(1)
	select {
	case err := <-r.fatal:
		if err == nil {
			t.Fatal("nonzero Execution exit did not report a fatal error")
		}
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fail after an Execution exit without fence proof")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.executionOwner != handle || r.executionUID != "test-incarnation" {
		t.Fatal("Runtime discarded the old Execution owner without fence proof")
	}
	if r.executionRecovering {
		t.Fatal("Runtime treated a nonzero exit as recoverable")
	}
}

func TestFencedExecutionLossDuringQuiesceDoesNotRestartAndClearsOwner(t *testing.T) {
	old := &recoveryTestHandle{
		exited: make(chan childExit, 1), fenced: make(chan struct{}),
	}
	var starts atomic.Int32
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionServer: old.Executor(), executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_QUIESCING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			starts.Add(1)
			return &recoveryTestHandle{
				fenced: make(chan struct{}),
			}, nil
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	old.exited <- childExit{code: 0}
	select {
	case <-old.fenced:
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fence the cleanly exited service while quiescing")
	}
	waitForExecutionOwner(t, r, nil)
	if got := starts.Load(); got != 0 {
		t.Fatalf("Runtime started %d replacements while quiescing; want none", got)
	}

	if err := r.startExecutionForUID(context.Background(), "test-incarnation"); err != nil {
		t.Fatalf("resume same incarnation after fenced quiesce: %v", err)
	}
	if got := starts.Load(); got != 1 {
		t.Fatalf("resume start count = %d, want one fresh service", got)
	}
	r.mu.Lock()
	got := r.executionOwner
	r.mu.Unlock()
	if got == nil || got == old {
		t.Fatal("same-incarnation resume reused the fenced service handle")
	}
	if err := got.StopAndFence(context.Background()); err != nil {
		t.Fatalf("fence replacement service: %v", err)
	}
}

func TestEmbeddedExecutionLossFencesBackendBeforeReplacement(t *testing.T) {
	events := make(chan string, 4)
	releaseFence := make(chan struct{})
	old := &executionServiceHandle{executor: &executorpb.UnimplementedExecutorServer{}, exited: make(chan childExit, 1)}
	old.fence = func(ctx context.Context) error {
		events <- "fence-start"
		select {
		case <-releaseFence:
			events <- "fence-complete"
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	var starts atomic.Int32
	var replacementHandle *executionServiceHandle
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionServer: old.Executor(), executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			starts.Add(1)
			replacementHandle = &executionServiceHandle{executor: &executorpb.UnimplementedExecutorServer{}, exited: make(chan childExit, 1)}
			events <- "replacement-ready"
			return replacementHandle, nil
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	old.exited <- childExit{}
	select {
	case event := <-events:
		if event != "fence-start" {
			t.Fatalf("first recovery event = %q, want fence-start", event)
		}
	case <-time.After(time.Second):
		t.Fatal("embedded Execution loss did not request a backend fence")
	}
	r.mu.Lock()
	ownerBeforeFence := r.executionOwner
	r.mu.Unlock()
	if ownerBeforeFence != old || starts.Load() != 0 {
		t.Fatal("Runtime replaced or released the embedded owner before backend fence proof")
	}
	close(releaseFence)
	for _, want := range []string{"fence-complete", "replacement-ready"} {
		select {
		case event := <-events:
			if event != want {
				t.Fatalf("recovery event = %q, want %q", event, want)
			}
		case <-time.After(time.Second):
			t.Fatalf("recovery did not reach %s", want)
		}
	}
	waitForExecutionOwner(t, r, replacementHandle)
	if got := starts.Load(); got != 1 {
		t.Fatalf("replacement count = %d, want one", got)
	}
}

func TestEmbeddedExecutionFenceFailureRetainsOwnerAndIsFatal(t *testing.T) {
	fenceFailure := errors.New("native executor cgroup remained populated")
	old := &executionServiceHandle{executor: &executorpb.UnimplementedExecutorServer{}, exited: make(chan childExit, 1), fence: func(context.Context) error {
		return fenceFailure
	}}
	starts := atomic.Int32{}
	r := &Runtime{
		config:         Config{CodexExecServer: &codexexecserver.Config{URL: "ws://executor.invalid"}},
		executionOwner: old, executionServer: old.Executor(), executionUID: "test-incarnation",
		intent:  &runtimepb.LifecycleIntent{Mode: runtimepb.LifecycleIntent_RUNNING, IncarnationUid: "test-incarnation"},
		changes: make(chan struct{}), fatal: make(chan error, 1),
		execCalls: make(map[uint64]context.CancelFunc), execStreams: make(map[uint64]context.CancelFunc),
		deps: runtimeDeps{startExecution: func(context.Context, Config, string) (executionHandle, error) {
			starts.Add(1)
			return nil, errors.New("must not replace an unfenced native executor")
		}},
	}
	go r.watchExecutionProcess(old, "test-incarnation", old)
	old.exited <- childExit{}
	select {
	case err := <-r.fatal:
		if !errors.Is(err, fenceFailure) {
			t.Fatalf("Runtime failure = %v, want fence failure %v", err, fenceFailure)
		}
	case <-time.After(time.Second):
		t.Fatal("Runtime did not fail after embedded backend fence failed")
	}
	r.mu.Lock()
	owner, uid := r.executionOwner, r.executionUID
	r.mu.Unlock()
	if owner != old || uid != "test-incarnation" {
		t.Fatal("Runtime released embedded ownership without process fence proof")
	}
	if got := starts.Load(); got != 0 {
		t.Fatalf("Runtime started %d replacements after fence failure, want none", got)
	}
}

func waitForExecutionOwner(t *testing.T, r *Runtime, want executionHandle) {
	t.Helper()
	deadline := time.NewTimer(time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		owner, recovering := r.executionOwner, r.executionRecovering
		r.mu.Unlock()
		if owner == want && !recovering {
			return
		}
		select {
		case <-ticker.C:
		case <-deadline.C:
			t.Fatalf("Execution owner/recovery did not settle: owner=%T recovering=%v", owner, recovering)
		}
	}
}
