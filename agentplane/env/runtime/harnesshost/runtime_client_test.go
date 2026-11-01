package harnesshost

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func boundaryHost(t *testing.T) (*Runner, context.Context) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	root := t.TempDir()
	r, err := New(Config{StateDir: root, Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(ctx); err != nil {
			t.Error(err)
		}
	})
	return r, ctx
}

func intentFor(r *Runner, generation uint64, mode runtimepb.LifecycleIntent_Mode) *runtimepb.LifecycleIntent {
	intent := &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime", HostBootId: r.bootID, Generation: generation}, SandboxUid: "sandbox", IncarnationUid: "incarnation", Mode: mode}
	if mode == runtimepb.LifecycleIntent_QUIESCING || mode == runtimepb.LifecycleIntent_CLOSING {
		intent.SealUid = intent.IncarnationUid
	}
	return intent
}

func bindingContext(ctx context.Context, binding *runtimepb.Binding) context.Context {
	return withBinding(ctx, binding)
}

func TestHostRejectsStaleGenerationBeforeDurableAdmission(t *testing.T) {
	r, ctx := boundaryHost(t)
	first := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, first); err != nil {
		t.Fatal(err)
	}
	next := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, next); err != nil {
		t.Fatal(err)
	}
	if _, err := r.BoundContext(ctx, first.Binding); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale direct-call ingress returned %v", err)
	}
	_, err := r.SubmitInput(bindingContext(ctx, first.Binding), &environmentruntimepb.SubmitInputRequest{SessionId: "never-created", CommandId: "stale", Text: "must not run"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale admission returned %v", err)
	}
	if ids, err := r.store.SessionIDs(); err != nil || len(ids) != 0 {
		t.Fatalf("stale request created state: %v, %v", ids, err)
	}
	changed := proto.Clone(next).(*runtimepb.LifecycleIntent)
	changed.Mode = runtimepb.LifecycleIntent_RUNNING
	changed.SealUid = ""
	if err := r.ApplyIntent(ctx, changed); err == nil {
		t.Fatal("mode changed without generation")
	}
}

func TestHostSealsOnlyAfterMatchingExecutionFence(t *testing.T) {
	r, ctx := boundaryHost(t)
	first := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, first); err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: r.config.StateDir, Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	s, err := r.sessions.Load(ctx, "session")
	if err != nil {
		t.Fatal(err)
	}
	// Runtime's retained marker is never read or cleared by Host.
	marker := filepath.Join(r.config.StateDir, "sessions", "sealed")
	if err := os.WriteFile(marker, []byte("runtime-owned"), 0600); err != nil {
		t.Fatal(err)
	}
	drain := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) { r.mu.Lock(); defer r.mu.Unlock(); return r.nativeStopped, nil }); err != nil {
		t.Fatal(err)
	}
	if s.EventJournal().SealState() != nil {
		t.Fatal("journal sealed before Execution fence")
	}
	stale := proto.Clone(first).(*runtimepb.LifecycleIntent)
	stale.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, stale); err == nil {
		t.Fatal("stale Execution fence accepted")
	}
	drain.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.wait(ctx, runnerlifecycle.Flushing); err != nil {
		t.Fatal(err)
	}
	seal := s.EventJournal().SealState()
	if seal == nil || seal.IncarnationUID != "incarnation" {
		t.Fatalf("seal=%+v", seal)
	}
	ack := retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}
	if err := r.store.WriteAcknowledgement("session", ack); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.publishAck("session", ack); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.wait(ctx, runnerlifecycle.Quiesced); err != nil {
		t.Fatal(err)
	}
	if contents, err := os.ReadFile(marker); err != nil || string(contents) != "runtime-owned" {
		t.Fatalf("Host mutated environment marker: %q, %v", contents, err)
	}
}

type bootstrapBindingRecorder struct {
	binding *runtimepb.Binding
}

func (c *bootstrapBindingRecorder) ConnectHost(ctx context.Context, _ *Runner) error {
	return ctx.Err()
}

func (c *bootstrapBindingRecorder) HostExecutionReady(context.Context) (bool, error) {
	return true, nil
}

func (c *bootstrapBindingRecorder) WaitHostBootstrap(_ context.Context, binding *runtimepb.Binding) error {
	c.binding = proto.Clone(binding).(*runtimepb.Binding)
	return nil
}

func TestAcceptedSetupKeepsRunningBootstrapBindingDuringDrain(t *testing.T) {
	r, ctx := boundaryHost(t)
	running := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, running); err != nil {
		t.Fatal(err)
	}
	recorder := &bootstrapBindingRecorder{}
	r.mu.Lock()
	r.runtimeBridge = recorder
	r.mu.Unlock()
	if err := r.ApplyIntent(ctx, intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)); err != nil {
		t.Fatal(err)
	}
	if err := r.waitBootstrap(ctx); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(recorder.binding, running.Binding) {
		t.Fatalf("accepted setup waited using draining binding: %v", recorder.binding)
	}
}
