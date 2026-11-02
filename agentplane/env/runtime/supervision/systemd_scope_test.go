package supervision

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

func TestSystemdScopeDeclarationHasNoNativeEffects(t *testing.T) {
	t.Setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	var workers sync.WaitGroup
	host := NewHost(t.Context(), store, processTestNamespace, RuntimeDomain, &workers, func(err error) { t.Error(err) })
	owner := SystemdOwner{Unit: "test-owner.service", LifetimeDirectory: filepath.Join(t.TempDir(), "absent"), ScopeID: "test-scope"}
	launcher := scopeTestLauncher()
	supervisor, err := host.DeclareSystemdScope(t.Context(), launcher, "/test-state", "test-sandbox-uid", owner)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(owner.LifetimeDirectory); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("declaration touched lifetime directory: %v", err)
	}
	process, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/test-binary"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(t.Context()); err == nil {
		t.Fatal("direct native launch bypassed unready scope")
	}
	if process.PID() != 0 {
		t.Fatalf("unready native process PID = %d", process.PID())
	}
	if _, err := host.DeclareSystemdScope(t.Context(), launcher, "/test-state", "test-sandbox-uid", owner); err == nil {
		t.Fatal("scope redeclaration accepted")
	}
}

func scopeTestLauncher() *SystemdLauncher {
	limits := ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 1024, PidsMax: 10, ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1}
	return &SystemdLauncher{Slice: WorkloadSlice, AgentUID: 1000, AgentGID: 1000, BlockDevices: []string{"/test-device"}, AggregateLimits: limits, ProcessLimits: limits}
}

func TestSystemdScopeReadinessWakesOnlyDependentProcessesAndSurvivesEngineRestart(t *testing.T) {
	t.Setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
	synctest.Test(t, func(t *testing.T) {
		lifetime, cancelLifetime := context.WithCancel(context.Background())
		defer cancelLifetime()
		store := state.WrapCore(namespaced.NewState(inmem.Build))
		var workers sync.WaitGroup
		host := NewHost(lifetime, store, processTestNamespace, RuntimeDomain, &workers, func(err error) { t.Error(err) })
		var allowFence atomic.Bool
		var fencedCalls, failedCalls atomic.Int32
		for _, scope := range []SystemdScopeID{"blocked", "independent"} {
			_, err := host.DeclareSystemdScope(lifetime, scopeTestLauncher(), "/test-state", "test-sandbox-uid", SystemdOwner{
				Unit: "test-owner.service", LifetimeDirectory: "/test-lifetime", ScopeID: scope,
			})
			if err != nil {
				t.Fatal(err)
			}
			host.systemdScopes[scope].reconcile = func(context.Context, SystemdScopeDesired) error {
				if scope == "blocked" && !allowFence.Load() {
					failedCalls.Add(1)
					return errors.New("test stale-unit fence is unproven")
				}
				fencedCalls.Add(1)
				return nil
			}
		}
		engineCtx, stopEngine := context.WithCancel(lifetime)
		startEngine := func(ctx context.Context) <-chan error {
			engine, err := runtime.NewRuntime(store, zap.NewNop())
			if err != nil {
				t.Fatal(err)
			}
			if err := engine.RegisterQController(host.SystemdScopeController()); err != nil {
				t.Fatal(err)
			}
			if err := engine.RegisterQController(host.Controller()); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() { done <- engine.Run(ctx) }()
			return done
		}
		done := startEngine(engineCtx)
		defer func() {
			stopEngine()
			if err := <-done; err != nil {
				t.Error(err)
			}
			cancelLifetime()
			workers.Wait()
		}()
		starts := make(map[SystemdScopeID]*atomic.Int32)
		for _, scope := range []SystemdScopeID{"blocked", "independent"} {
			starts[scope] = &atomic.Int32{}
			if err := host.requestNative(lifetime, string(scope), DesiredState{Owner: "test-owner", SystemdScope: scope, StopTimeout: time.Second}, func(context.Context) (*Instance, error) {
				starts[scope].Add(1)
				return &Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
			}); err != nil {
				t.Fatal(err)
			}
		}
		synctest.Wait()
		if starts["blocked"].Load() != 0 || starts["independent"].Load() != 1 {
			t.Fatalf("starts before fencing: blocked=%d independent=%d", starts["blocked"].Load(), starts["independent"].Load())
		}
		status, found, err := ReadSystemdScopeStatus(lifetime, store, processTestNamespace, "blocked")
		if err != nil || !found || status.Ready || status.Error == "" || failedCalls.Load() == 0 {
			t.Fatalf("failed scope status = %+v found=%t error=%v", status, found, err)
		}
		allowFence.Store(true)
		// Waiting on the observable start allows COSI's own retry timer to advance.
		_, err = store.WatchFor(lifetime, resourceFor(processTestNamespace, "blocked", Status{}).Metadata(), state.WithCondition(func(resource.Resource) (bool, error) {
			status, _, err := ReadStatus(lifetime, store, processTestNamespace, "blocked")
			return status.Phase == Running, err
		}))
		if err != nil {
			t.Fatal(err)
		}
		if starts["blocked"].Load() != 1 || fencedCalls.Load() != 2 {
			t.Fatalf("readiness did not release one attempt per scope: starts=%d fences=%d", starts["blocked"].Load(), fencedCalls.Load())
		}
		stopEngine()
		if err := <-done; err != nil {
			t.Fatal(err)
		}
		// Losing status must not cause a restarted controller to inventory live units.
		if err := store.Destroy(lifetime, resourceFor(processTestNamespace, "blocked", SystemdScopeStatus{}).Metadata(), state.WithDestroyOwner(host.SystemdScopeController().Name())); err != nil {
			t.Fatal(err)
		}
		engineCtx, stopEngine = context.WithCancel(lifetime)
		done = startEngine(engineCtx)
		synctest.Wait()
		status, found, err = ReadSystemdScopeStatus(lifetime, store, processTestNamespace, "blocked")
		if err != nil || !found || !status.Ready || status.Error != "" || fencedCalls.Load() != 2 {
			t.Fatalf("restarted scope status = %+v found=%t error=%v fences=%d", status, found, err, fencedCalls.Load())
		}
		for _, scope := range []SystemdScopeID{"blocked", "independent"} {
			if starts[scope].Load() != 1 {
				t.Fatalf("engine restart replayed scope %q launch", scope)
			}
			if _, err := store.Teardown(lifetime, resourceFor(processTestNamespace, string(scope), DesiredState{}).Metadata(), state.WithTeardownOwner(hostOwner)); err != nil {
				t.Fatal(err)
			}
		}
		synctest.Wait()
	})
}
