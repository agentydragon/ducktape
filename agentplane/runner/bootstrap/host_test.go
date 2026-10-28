package bootstrap

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const bootstrapTestTimeout = 10 * time.Second

type hostTestRuntime struct {
	ctx       context.Context
	cancel    context.CancelFunc
	state     state.State
	host      *Host
	workers   *sync.WaitGroup
	done      chan struct{}
	failures  chan error
	closeOnce sync.Once
}

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == supervision.SupervisorMode {
		os.Exit(supervision.SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func newHostTestRuntime(t *testing.T, script *string, stateDir string, admission lifecycle.AdmissionGuard) *hostTestRuntime {
	t.Helper()

	ctx, cancel := context.WithCancel(context.Background())
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	workers := &sync.WaitGroup{}
	failures := make(chan error, 8)
	reportError := func(err error) {
		if err == nil {
			return
		}
		select {
		case failures <- err:
		default:
		}
	}
	processes := supervision.NewHost(ctx, store, lifecycle.Namespace, workers, reportError)
	supervisor := supervision.NewSubprocessSupervisor(nil)
	scriptRunner, err := scripts.New(scripts.Config{
		LaunchProcess: func(ctx context.Context, owner string, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return processes.Launch(ctx, owner, 1, supervisor, config)
		},
	})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if admission == nil {
		admission = func(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadDesired(ctx, store)
			if err != nil {
				return err
			}
			if !found || current != expected || current.Mode != lifecycle.Running {
				return nil
			}
			return effect(ctx)
		}
	}
	host, err := NewHost(Config{Script: script, StateDir: stateDir}, Dependencies{
		Context: ctx, State: store, Workers: workers, Scripts: scriptRunner,
		Admission: admission, OnError: reportError,
	})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := lifecycle.PublishDesired(ctx, store, lifecycle.Desired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := host.Initialize(ctx); err != nil {
		cancel()
		t.Fatal(err)
	}
	engine, err := runtime.NewRuntime(store, zap.L().Named("bootstrap-component-test"))
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := engine.RegisterQController(host.Controller()); err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := engine.RegisterQController(processes.Controller()); err != nil {
		cancel()
		t.Fatal(err)
	}
	testRuntime := &hostTestRuntime{
		ctx: ctx, cancel: cancel, state: store, host: host, workers: workers,
		done: make(chan struct{}), failures: failures,
	}
	go func() {
		defer close(testRuntime.done)
		if err := engine.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			reportError(err)
		}
	}()
	t.Cleanup(func() { testRuntime.close(t) })
	return testRuntime
}

func (r *hostTestRuntime) close(t *testing.T) {
	t.Helper()
	r.closeOnce.Do(func() {
		r.cancel()
		<-r.done
		r.workers.Wait()
		if err := r.host.CloseLog(); err != nil {
			t.Errorf("close bootstrap log: %v", err)
		}
		for {
			select {
			case err := <-r.failures:
				t.Errorf("bootstrap test runtime failed: %v", err)
			default:
				return
			}
		}
	})
}

func TestWaitCancellationDoesNotCancelAttemptAndDurableSuccessDoesNotRerun(t *testing.T) {
	stateDir := t.TempDir()
	started := filepath.Join(stateDir, "started")
	release := filepath.Join(stateDir, "release")
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf(": > %q\nwhile [ ! -f %q ]; do sleep 0.01; done\nprintf b >> %q\nprintf ready", started, release, count)
	first := newHostTestRuntime(t, &script, stateDir, nil)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })

	waitCtx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := first.host.Wait(waitCtx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}
	waitForFile(t, started)
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := first.host.Wait(ctx); err != nil {
		t.Fatalf("wait for admitted bootstrap: %v", err)
	}
	if err := first.host.CloseLog(); err != nil {
		t.Fatal(err)
	}
	log, err := OpenLog(filepath.Join(stateDir, "initialization", "events.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	events := log.Since(0)
	if err := log.Close(); err != nil {
		t.Fatal(err)
	}
	if len(events) < 2 || events[len(events)-1].GetResult() == nil || events[len(events)-1].GetResult().ExitCode != 0 {
		t.Fatalf("durable bootstrap events = %v, want output and successful result", events)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count = %q, %v; want one execution", data, err)
	}
	first.close(t)

	second := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel = context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := second.host.Wait(ctx); err != nil {
		t.Fatalf("restore completed bootstrap outcome: %v", err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count after recovery = %q, %v; want no rerun", data, err)
	}
	runtimeState, found, err := ReadRuntime(ctx, second.state)
	if err != nil || !found || runtimeState.Phase != Succeeded {
		t.Fatalf("restored bootstrap runtime = (%+v, found=%t, err=%v), want succeeded", runtimeState, found, err)
	}
}

func TestInterruptedAttemptIsReconstructedWithoutRerunning(t *testing.T) {
	stateDir := t.TempDir()
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf("printf b >> %q", count)
	root := filepath.Join(stateDir, "initialization")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(script))
	request, err := json.Marshal(struct {
		SHA string `json:"script_sha256"`
	}{hex.EncodeToString(digest[:])})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "request.json"), request, 0o600); err != nil {
		t.Fatal(err)
	}
	log, err := OpenLog(filepath.Join(root, "events.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendInterrupted(1); err != nil {
		t.Fatal(err)
	}
	if err := log.Close(); err != nil {
		t.Fatal(err)
	}

	r := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); !errors.Is(err, ErrInterrupted) {
		t.Fatalf("restored interrupted bootstrap error = %v, want ErrInterrupted", err)
	}
	if _, err := os.Stat(count); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("interrupted bootstrap script ran again: %v", err)
	}
	runtimeState, found, err := ReadRuntime(ctx, r.state)
	if err != nil || !found || runtimeState.Phase != Interrupted {
		t.Fatalf("restored interrupted runtime = (%+v, found=%t, err=%v), want interrupted", runtimeState, found, err)
	}
}

func TestRequestDetectsAdmissionGuardThatSkippedEffect(t *testing.T) {
	stateDir := t.TempDir()
	script := "true"
	r := newHostTestRuntime(t, &script, stateDir, func(context.Context, lifecycle.Desired, func(context.Context) error) error {
		return nil
	})
	if err := r.host.Request(r.ctx); err == nil {
		t.Fatal("request succeeded although the admission guard skipped its effect")
	} else if code := status.Code(err); code != codes.Aborted {
		t.Fatalf("skipped admission error code = %s, want Aborted: %v", code, err)
	}
	input, found, err := ReadInput(r.ctx, r.state)
	if err != nil || !found || input.Requested {
		t.Fatalf("input after skipped admission = (%+v, found=%t, err=%v), want unrequested", input, found, err)
	}
	if err := publish(r.ctx, r.state, ResourceID, Input{Requested: true}); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.PublishDesired(r.ctx, r.state, lifecycle.Desired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	if err := r.host.Request(r.ctx); err != nil {
		t.Fatalf("repeat of already accepted request during Close: %v", err)
	}
}

func TestDecodeSelectionRemainsStrict(t *testing.T) {
	for _, test := range []struct {
		name     string
		data     string
		rejected bool
	}{
		{name: "valid", data: `{"script_sha256":"abc"}`},
		{name: "unknown field", data: `{"script_sha256":"abc","extra":true}`, rejected: true},
		{name: "trailing value", data: `{"script_sha256":"abc"} {}`, rejected: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			var selection struct {
				SHA string `json:"script_sha256"`
			}
			err := decodeSelection([]byte(test.data), &selection)
			if !test.rejected && (err != nil || selection.SHA != "abc") {
				t.Fatalf("decode valid selection = %+v, %v", selection, err)
			}
			if test.rejected && err == nil {
				t.Fatal("decode accepted non-strict selection")
			}
		})
	}
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(bootstrapTestTimeout)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", path)
}
