package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
)

func TestBootstrapIsActiveUntilAcceptedInputIsTerminal(t *testing.T) {
	for _, tc := range []struct {
		name       string
		input      bootstrap.Input
		inputFound bool
		state      bootstrap.Runtime
		stateFound bool
		wantActive bool
	}{
		{name: "not requested", inputFound: true, state: bootstrap.Runtime{Phase: bootstrap.Dormant}, stateFound: true},
		{name: "accepted before controller runs", input: bootstrap.Input{Requested: true}, inputFound: true, wantActive: true},
		{name: "running", input: bootstrap.Input{Requested: true}, inputFound: true, state: bootstrap.Runtime{Phase: bootstrap.Running}, stateFound: true, wantActive: true},
		{name: "succeeded", input: bootstrap.Input{Requested: true}, inputFound: true, state: bootstrap.Runtime{Phase: bootstrap.Succeeded}, stateFound: true},
		{name: "failed", input: bootstrap.Input{Requested: true}, inputFound: true, state: bootstrap.Runtime{Phase: bootstrap.Failed}, stateFound: true},
		{name: "interrupted", input: bootstrap.Input{Requested: true}, inputFound: true, state: bootstrap.Runtime{Phase: bootstrap.Interrupted}, stateFound: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := bootstrapIsActive(tc.input, tc.inputFound, tc.state, tc.stateFound); got != tc.wantActive {
				t.Fatalf("bootstrapIsActive() = %t, want %t", got, tc.wantActive)
			}
		})
	}
}

func TestCanceledBootstrapWaiterDoesNotCancelWorkAndCloseWaits(t *testing.T) {
	stateDir := t.TempDir()
	workspace := t.TempDir()
	started := filepath.Join(stateDir, "bootstrap-started")
	release := filepath.Join(stateDir, "bootstrap-release")
	count := filepath.Join(stateDir, "bootstrap-count")
	script := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf b >> %s", shellQuote(started), shellQuote(release), shellQuote(count))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &script, "/bin/false")
	config.StateDir = stateDir
	r := newLifecycleRunner(t, config)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })

	waitForFile(t, started)
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.Bootstrap != runnerstatepb.RunnerFacts_BOOTSTRAP_RUNNING || !hasAutosuspendReason(facts.AutosuspendBlockers, "Bootstrap") || facts.IdleSince != nil {
		t.Fatalf("running bootstrap facts = %+v, want Bootstrap blocker and no idle timestamp", facts)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.bootstrapHost.Wait(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Runtime close request")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned while the accepted bootstrap was running: %v", err)
	default:
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}

	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap effect after close = %q, %v; want one execution", data, err)
	}

	restarted := newLifecycleRunner(t, config)
	ctx, cancel = context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	waitRuntime(t, restarted, func() bool { settled, err := restarted.bootstrapHost.Settled(ctx); return err == nil && settled }, "bootstrap settlement")
	if err := restarted.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap effect after restart = %q, %v; want one execution", data, err)
	}
}

func TestBootstrapRuntimeFailureDoesNotLeaveLogDerivedBusyFact(t *testing.T) {
	stateDir := t.TempDir()
	workspace := t.TempDir()
	started := filepath.Join(stateDir, "bootstrap-started")
	release := filepath.Join(stateDir, "bootstrap-release")
	script := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf output", shellQuote(started), shellQuote(release))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &script, "/bin/false")
	config.StateDir = stateDir
	r := newLifecycleRunner(t, config)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })
	waitForFile(t, started)

	if err := r.bootstrapHost.CloseLog(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	waitRuntime(t, r, func() bool { settled, err := r.bootstrapHost.Settled(ctx); return err == nil && settled }, "failed bootstrap settlement")

	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.Bootstrap != runnerstatepb.RunnerFacts_BOOTSTRAP_FAILED {
		t.Fatalf("bootstrap fact = %v, want failed", facts.Bootstrap)
	}
	for _, blocker := range facts.AutosuspendBlockers {
		if blocker.GetReason() == "Bootstrap" {
			t.Fatal("terminal bootstrap resource still blocks autosuspend")
		}
	}
	runtime, found, err := bootstrap.ReadRuntime(ctx, r.lifecycleState)
	if err != nil || !found || runtime.Phase != bootstrap.Failed || !strings.Contains(runtime.Error, "initialization log is closed") {
		t.Fatalf("bootstrap runtime after log write failure = (%+v, found=%t, err=%v), want concrete storage failure", runtime, found, err)
	}
}
