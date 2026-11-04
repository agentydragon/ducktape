package runtime

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

func TestRuntimeExecutionServiceConfigCarriesResolvedReadiness(t *testing.T) {
	want := codexexecserver.Readiness{Timeout: 725 * time.Millisecond, ProbeTimeout: 55 * time.Millisecond, ProbeInterval: 130 * time.Millisecond}
	config := Config{
		StateDir: "/state", Supervision: "pid1",
		Launch:             configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-sandbox-uid"}},
		HarnessEnvironment: map[string]string{"PATH": "/usr/bin"},
		CodexExecServer: &codexexecserver.Config{
			URL: "ws://127.0.0.1:4501", ContainerSocket: "/run/executor.sock", Readiness: want,
		},
	}
	resolved, err := runtimeExecutionServiceConfig(config, "test-incarnation", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Readiness != want {
		t.Fatalf("embedded service readiness = %+v, want configured values %+v", resolved.Readiness, want)
	}
}

func TestRuntimeExecutionReadyTimeoutPreservesConfiguredWindow(t *testing.T) {
	for _, test := range []struct {
		name        string
		probeWindow time.Duration
		want        time.Duration
	}{
		{name: "default window plus setup", probeWindow: 20 * time.Second, want: 140 * time.Second},
		{name: "override above former fixed cap", probeWindow: 45 * time.Second, want: 165 * time.Second},
		{name: "saturates instead of overflowing", probeWindow: time.Duration(1<<63 - 1), want: time.Duration(1<<63 - 1)},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := executionServiceReadyTimeout(test.probeWindow); got != test.want {
				t.Fatalf("runtime Execution readiness timeout = %s, want %s", got, test.want)
			}
		})
	}
}

func TestSupervisedExecutionFenceProofSurvivesCleanupRetry(t *testing.T) {
	managerOpen := true
	fenceCalls := 0
	handle := &executionServiceHandle{
		fence: func(context.Context) error {
			fenceCalls++
			if !managerOpen {
				return errors.New("manager connection is already closed")
			}
			managerOpen = false
			return nil
		},
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("first fence: %v", err)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("retry after successful fence: %v", err)
	}
	if fenceCalls != 1 {
		t.Fatalf("fence calls = %d, want cached proof after one successful attempt", fenceCalls)
	}
}

func TestEmbeddedExecutionBackendLossRequiresFencedResult(t *testing.T) {
	for _, test := range []struct {
		name string
		err  error
		code int
	}{
		{name: "fenced backend loss", err: service.ErrFencedBackendLoss, code: 0},
		{name: "unproven backend loss", err: errors.New("native fence failed"), code: 1},
		{name: "unrequested clean exit", code: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			handle := newExecutionServiceHandle(service.Config{}, func(context.Context, service.Config, func(executorpb.ExecutorServer, context.Context)) error {
				return test.err
			})
			exit := <-handle.ProcessExit()
			if exit.code != test.code {
				t.Fatalf("exit code = %d, want %d (error: %v)", exit.code, test.code, exit.err)
			}
			if exit.code == 0 && exit.err != nil {
				t.Fatalf("fenced exit returned error: %v", exit.err)
			}
			if exit.code != 0 && exit.err == nil {
				t.Fatal("unproven exit returned no error")
			}
		})
	}
}

func TestFencedExecutionBackendLossLogsCauseAndRemainsRecoverable(t *testing.T) {
	core, observed := observer.New(zap.WarnLevel)
	undo := zap.ReplaceGlobals(zap.New(core))
	t.Cleanup(undo)

	const lossReason = "execution backend lost\nCodex exec-server exited: code=137"
	backendLoss := errors.Join(service.ErrFencedBackendLoss, errors.New(lossReason))
	config := service.Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", Backend: "systemd",
	}
	handle := newExecutionServiceHandle(config, func(context.Context, service.Config, func(executorpb.ExecutorServer, context.Context)) error {
		return backendLoss
	})
	exit := <-handle.ProcessExit()
	if exit.code != 0 || exit.err != nil {
		t.Fatalf("fenced backend loss = %+v, want clean recoverable exit", exit)
	}

	entries := observed.FilterMessage("embedded Execution backend loss fenced").All()
	if len(entries) != 1 {
		t.Fatalf("backend loss log entries = %d, want 1", len(entries))
	}
	fields := entries[0].ContextMap()
	for key, want := range map[string]string{
		"sandbox_namespace": "test-namespace",
		"sandbox_name":      "test-sandbox",
		"sandbox_uid":       "test-sandbox-uid",
		"incarnation_uid":   "test-incarnation",
		"backend":           "systemd",
		"loss_reason":       backendLoss.Error(),
	} {
		if got := fields[key]; got != want {
			t.Errorf("backend loss log %s = %v, want %q", key, got, want)
		}
	}
}

func TestEmbeddedExecutionStopJoinsServiceBeforeFenceProof(t *testing.T) {
	allowFence := make(chan struct{})
	handle := newExecutionServiceHandle(service.Config{}, func(ctx context.Context, _ service.Config, _ func(executorpb.ExecutorServer, context.Context)) error {
		<-ctx.Done()
		<-allowFence
		return nil
	})
	probe, cancel := context.WithCancel(context.Background())
	cancel()
	if err := handle.StopAndFence(probe); !errors.Is(err, context.Canceled) {
		t.Fatalf("unfinished service wait = %v, want cancellation", err)
	}
	close(allowFence)
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("joined service fence = %v, want clean fence", err)
	}
}

func TestSupervisedExecutionFenceRetriesAfterUnprovenAttempt(t *testing.T) {
	fenceErr := errors.New("unit process domain remains populated")
	fenceCalls := 0
	handle := &executionServiceHandle{fence: func(context.Context) error {
		fenceCalls++
		if fenceCalls == 1 {
			return fenceErr
		}
		return nil
	}}
	if err := handle.StopAndFence(context.Background()); !errors.Is(err, fenceErr) {
		t.Fatalf("first fence = %v, want %v", err, fenceErr)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("retry fence: %v", err)
	}
	if err := handle.StopAndFence(context.Background()); err != nil {
		t.Fatalf("cached successful fence: %v", err)
	}
	if fenceCalls != 2 {
		t.Fatalf("fence calls = %d, want failed attempt plus successful proof", fenceCalls)
	}
}
