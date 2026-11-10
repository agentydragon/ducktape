package runtime

import (
	"context"
	"errors"
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"go.etcd.io/bbolt"
)

func TestRuntimeOpensDatabaseBeforePreparingSupervision(t *testing.T) {
	t.Setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	config.Supervision = "systemd"
	config.PodPID1 = false
	limits := supervision.ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 1024, PidsMax: 10, ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1}
	config.VMProcessLauncher = &supervision.SystemdLauncher{
		Slice: supervision.WorkloadSlice, AgentUID: 1000, AgentGID: 1000,
		BlockDevices: []string{"/test-device"}, AggregateLimits: limits, ProcessLimits: limits,
	}
	preparations := 0
	deps := runtimeDeps{prepareSupervisor: func(_ Config) (*supervision.Supervisor, error) {
		preparations++
		_, store, err := cosistate.Open(config.StateDir, lifecycle.Namespace)
		if store != nil {
			_ = store.Close()
		}
		if !errors.Is(err, bbolt.ErrTimeout) {
			return nil, fmt.Errorf("database not locked during supervision preparation: %w", err)
		}
		return supervision.NewPID1Supervisor()
	}}
	r, err := newRuntime(config.Config, nil, deps)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.Close(context.Background()) })
	for _, scope := range []supervision.SystemdScopeID{RuntimeScopeID, HarnessScopeID} {
		if status, found, err := supervision.ReadSystemdScopeStatus(t.Context(), r.lifecycleState, lifecycle.Namespace, scope); err != nil || found {
			t.Fatalf("scope reconciled before engine startup: scope=%q status=%+v found=%t err=%v", scope, status, found, err)
		}
	}
	if _, err := newRuntime(config.Config, nil, deps); !errors.Is(err, bbolt.ErrTimeout) {
		t.Fatalf("second Runtime error = %v, want database lock timeout", err)
	}
	if preparations != 1 {
		t.Fatalf("supervision prepared %d times, want once", preparations)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	_, store, err := cosistate.Open(config.StateDir, lifecycle.Namespace)
	if err != nil {
		t.Fatalf("database remained locked after shutdown: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeReleasesDatabaseWhenSupervisionPreparationFails(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	want := errors.New("supervision preparation failed")
	deps := runtimeDeps{prepareSupervisor: func(Config) (*supervision.Supervisor, error) {
		return nil, want
	}}
	if _, err := newRuntime(config.Config, nil, deps); !errors.Is(err, want) {
		t.Fatalf("new Runtime error = %v, want %v", err, want)
	}
	_, store, err := cosistate.Open(config.StateDir, lifecycle.Namespace)
	if err != nil {
		t.Fatalf("database remained locked after failed startup: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
}
