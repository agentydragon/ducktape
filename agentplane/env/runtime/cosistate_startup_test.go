package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"go.etcd.io/bbolt"
)

func TestRuntimeOpensDatabaseBeforePreparingSupervision(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	config.Supervision = "systemd"
	config.VMProcessLauncher = &supervision.SystemdLauncher{}
	preparations := 0
	deps := runtimeDeps{prepareSupervisor: func(_ Config, owner *os.File) (*supervision.Supervisor, error) {
		preparations++
		_, store, err := cosistate.Open(config.StateDir, lifecycle.Namespace)
		if store != nil {
			_ = store.Close()
		}
		if !errors.Is(err, bbolt.ErrTimeout) {
			return nil, fmt.Errorf("database not locked during supervision preparation: %w", err)
		}
		return supervision.NewSubprocessSupervisor(owner), nil
	}}
	r, err := newRuntime(config.Config, nil, deps)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.Close(context.Background()) })
	if r.owner != nil {
		t.Fatal("systemd Runtime acquired an additional owner fence")
	}
	if _, err := os.Stat(filepath.Join(config.StateDir, environmentRuntimeOwnerName)); !os.IsNotExist(err) {
		t.Fatalf("systemd Runtime created an owner file: %v", err)
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
	deps := runtimeDeps{prepareSupervisor: func(Config, *os.File) (*supervision.Supervisor, error) {
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
