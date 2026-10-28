package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
	if err := r.bootstrap(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close() }()
	waitLifecycle(t, r, func() bool { return r.lifecycle.goal().Mode == runnerClosing }, "runner close request")
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
		return restarted.bootstrapSettled(), nil
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

func TestBootstrapRunErrorWritesInterruptedOutcome(t *testing.T) {
	r := newBareRunner(t)
	script := "printf output"
	r.config.Launch.Bootstrap = &script
	r.config.InitializationCwd = t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := r.runBootstrap(ctx)
	if err == nil || !errors.Is(err, errBootstrapInterrupted) {
		t.Fatalf("canceled native bootstrap error = %v, want an interrupted outcome", err)
	}
	r.bootstrapMu.Lock()
	log := r.initLog
	r.bootstrapMu.Unlock()
	if log == nil || !log.Ended() {
		t.Fatalf("native bootstrap failure left a nonterminal initialization log: %v", log)
	}
	last := log.Since(log.LastSequence() - 1)
	if len(last) != 1 || last[0].GetInterrupted() == nil {
		t.Fatalf("terminal initialization event = %v, want interrupted", last)
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

	r.bootstrapMu.Lock()
	log := r.initLog
	r.bootstrapMu.Unlock()
	if log == nil {
		t.Fatal("bootstrap log was not opened before the script started")
	}
	if err := log.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) { return r.bootstrapSettled(), nil }); err != nil {
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
	runtime, found, err := lifecycleRead[bootstrapRuntimeSpec](r.ctx, r.lifecycle.state, bootstrapRuntimeType, "bootstrap")
	if err != nil || !found || runtime.Phase != bootstrapFailed || !strings.Contains(runtime.Error, "initialization log is closed") {
		t.Fatalf("bootstrap runtime after log write failure = (%+v, found=%t, err=%v), want concrete storage failure", runtime, found, err)
	}
}
