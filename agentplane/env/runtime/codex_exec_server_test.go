package runtime

import (
	"context"
	"reflect"
	"sync"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestExecutionSessionAdmissionClosesWhenRuntimeQuiesces(t *testing.T) {
	url, _, _ := startNativeExecutionTestServer(t, false)
	r := newExecutionRuntime(t, url, nil)
	client := startDirectExecutionTestClient(t, r, "incarnation")
	if _, err := r.Quiesce(client.ctx, &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	stream, err := client.open(client.callCtx, "late-session", "late-open")
	if err != nil {
		t.Fatal(err)
	}
	result, err := stream.Recv()
	if err == nil && result.GetStatus().GetOutcome() == executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatal("quiescing environment admitted execution")
	}
}

func TestExecutionSessionsBlockAutosuspendUntilClosed(t *testing.T) {
	started := make(chan struct{})
	r := newExecutionRuntime(t, "ws://unused", func(ctx context.Context) (string, error) { close(started); <-ctx.Done(); return "", ctx.Err() })
	client := startDirectExecutionTestClient(t, r, "incarnation")
	ctx, cancel := context.WithCancel(client.callCtx)
	stream, err := client.open(ctx, "sampler-session", "blocked-readiness")
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-client.ctx.Done():
		t.Fatal("readiness did not start")
	}
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	blocked := false
	for _, blocker := range facts.AutosuspendBlockers {
		blocked = blocked || blocker.Reason == "ExecutorRelay"
	}
	if !blocked || facts.IdleSince != nil {
		t.Fatalf("active proxy session did not block autosuspend: %+v", facts)
	}
	cancel()
	_, _ = stream.Recv()
	waitRuntime(t, r, func() bool { facts, err := r.facts(); return err == nil && facts.IdleSince != nil }, "execution stream idle")
}

// A replacement incarnation cannot use a new Execution owner until the prior
// binding has stopped native work and its service has proved the process fence.
func TestIncarnationReplacementFencesExecutionBeforeStartingSuccessor(t *testing.T) {
	config := executionRuntimeConfig(t, "ws://unused", nil)
	start := config.deps.startExecution
	entered, release := make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	releaseFence := func() { releaseOnce.Do(func() { close(release) }) }
	defer releaseFence()
	var mu sync.Mutex
	var events []string
	record := func(event string) { mu.Lock(); events = append(events, event); mu.Unlock() }
	config.deps.startExecution = func(ctx context.Context, config Config, uid string) (executionHandle, error) {
		record("start:" + uid)
		handle, err := start(ctx, config, uid)
		if err != nil {
			return nil, err
		}
		return &recordingExecutionHandle{executionHandle: handle, beforeFence: func(ctx context.Context) error {
			record("fence:" + uid)
			if uid == "test-incarnation" {
				close(entered)
				select {
				case <-release:
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			return nil
		}}, nil
	}
	r := newLifecycleRunner(t, config)
	done := make(chan error, 1)
	go func() { done <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("prior Execution fence was not requested")
	}
	mu.Lock()
	before := append([]string(nil), events...)
	mu.Unlock()
	if !reflect.DeepEqual(before, []string{"start:test-incarnation", "fence:test-incarnation"}) {
		t.Errorf("successor started before the prior fence: %v", before)
	}
	if uid, ok := r.state.IncarnationUID(); !ok || uid != "test-incarnation" {
		t.Errorf("incarnation changed before fence: %q (%v)", uid, ok)
	}
	releaseFence()
	if err := waitClose(t, done); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	after := append([]string(nil), events...)
	mu.Unlock()
	if !reflect.DeepEqual(after, []string{"start:test-incarnation", "fence:test-incarnation", "start:successor"}) {
		t.Fatalf("replacement ordering: %v", after)
	}
}

type recordingExecutionHandle struct {
	executionHandle
	beforeFence func(context.Context) error
}

func (h *recordingExecutionHandle) StopAndFence(ctx context.Context) error {
	if err := h.beforeFence(ctx); err != nil {
		return err
	}
	return h.executionHandle.StopAndFence(ctx)
}
