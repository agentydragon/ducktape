package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
)

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
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.bootstrapHost.Wait(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close() }()
	waitLifecycle(t, r, func() bool { return r.lifecycle.desiredState().Mode == runnerlifecycle.Closing }, "runner close request")
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
	if err := restarted.lifecycle.waitFor(ctx, func() (bool, error) {
		return restarted.bootstrapHost.Settled(ctx)
	}); err != nil {
		t.Fatal(err)
	}
	if err := restarted.Close(); err != nil {
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
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) { return r.bootstrapHost.Settled(ctx) }); err != nil {
		t.Fatal(err)
	}

	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.Bootstrap != controlpb.RunnerFacts_BOOTSTRAP_FAILED {
		t.Fatalf("bootstrap fact = %v, want failed", facts.Bootstrap)
	}
	for _, blocker := range facts.AutosuspendBlockers {
		if blocker.GetReason() == autosuspendBootstrap {
			t.Fatal("terminal bootstrap resource still blocks autosuspend")
		}
	}
	runtime, found, err := bootstrap.ReadRuntime(r.ctx, r.lifecycle.state)
	if err != nil || !found || runtime.Phase != bootstrap.Failed || !strings.Contains(runtime.Error, "initialization log is closed") {
		t.Fatalf("bootstrap runtime after log write failure = (%+v, found=%t, err=%v), want concrete storage failure", runtime, found, err)
	}
}
