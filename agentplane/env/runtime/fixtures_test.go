package runtime

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/state"
	"golang.org/x/sys/unix"
)

const lifecycleTestTimeout = 10 * time.Second

func mustRuntimeCloseStatus(t *testing.T, r *Runtime) RuntimeCloseStatus {
	t.Helper()
	status, _, err := readRuntimeCloseStatus(t.Context(), r.lifecycleState)
	if err != nil {
		t.Fatal(err)
	}
	return status
}

func waitRuntimeCloseStatus(t *testing.T, r *Runtime, want func(RuntimeCloseStatus) bool, description string) RuntimeCloseStatus {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	events := make(chan state.Event, 1)
	if err := r.lifecycleState.Watch(ctx, runtimeCloseStatusResource(RuntimeCloseStatus{}).Metadata(), events); err != nil {
		t.Fatal(err)
	}
	for {
		status, found, err := readRuntimeCloseStatus(ctx, r.lifecycleState)
		if err != nil {
			t.Fatal(err)
		}
		if found && want(status) {
			return status
		}
		select {
		case event, ok := <-events:
			if !ok || event.Type == state.Errored {
				t.Fatalf("watch %s: closed or failed: %+v", description, event)
			}
		case <-ctx.Done():
			t.Fatalf("wait for %s: %v", description, ctx.Err())
		}
	}
}

func TestMain(m *testing.M) {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case workspace.HelperMode:
			os.Exit(workspace.Main(os.Args[2:]))
		}
	}
	os.Exit(m.Run())
}

type runtimeTestConfig struct {
	Config
	deps             runtimeDeps
	expectCloseError bool
	testBacking      io.Closer
}

func lifecycleConfig(t *testing.T, endpoint, workspace string, bootstrap *string, binary string) runtimeTestConfig {
	t.Helper()
	root := t.TempDir()
	launch := configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}, Bootstrap: bootstrap}
	config := Config{StateDir: root, Launch: launch, Supervision: "pid1", PodPID1: true, InitializationCwd: workspace, Control: insecureControl(), WorkspaceRoot: workspace, Claude: &harness.LaunchConfig{Binary: binary, BaseURL: endpoint, Token: "test"}, ModelConfigClient: fixtureModelConfig{endpoint: endpoint}}
	return runtimeTestConfig{Config: config}
}

func insecureControl() configuration.EnvironmentRuntimeControlConfiguration {
	return configuration.EnvironmentRuntimeControlConfiguration{ListenAddress: "127.0.0.1:0", Security: environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}}
}

// The fixture exercises the production Runtime and its shared component controllers.
func newLifecycleRuntime(t *testing.T, config runtimeTestConfig) *Runtime {
	t.Helper()
	return newLifecycleRunnerWithState(t, config, nil)
}

func newLifecycleRunnerWithState(t *testing.T, config runtimeTestConfig, store state.State) *Runtime {
	t.Helper()
	if config.Config.HistoryIngress.Target == "" {
		config.Config.HistoryIngress.Target = startAcknowledgingHistory(t)
	}
	var r *Runtime
	var err error
	if config.testBacking != nil {
		r, err = newRuntime(config.Config, store, config.deps)
		if err == nil {
			r.lifecycleStore = config.testBacking
		}
	} else {
		r, err = newTestRuntime(t, config.Config, store, config.deps)
	}
	if err != nil {
		t.Fatal(err)
	}
	if err := r.startSessions(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer closeCancel()
		closeErr := r.Close(closeCtx)
		if (closeErr != nil) != config.expectCloseError {
			t.Errorf("Runtime close error=%v, expected error=%v", closeErr, config.expectCloseError)
		}
		if config.expectCloseError && closeErr != nil {
			// An unproved fence deliberately retains the shared engine and owner.
			// This fixture owns only temporary state. Settle Session cleanup
			// before stopping the engine, even though production retains the owner.
			select {
			case <-r.lifecycleDone:
			default:
				_ = r.stopSessions(closeCtx)
			}
			_ = r.stopIngress(closeCtx)
			r.lifecycleCancel()
			<-r.lifecycleDone
			_ = r.closeSessions(closeCtx)
			// The real outer container fence runs when a failed Runtime exits. This
			// suite shares PID 1 across cases, so perform that fence before the next case.
			if err := unix.Kill(-1, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
				t.Errorf("kill test namespace: %v", err)
			}
			for {
				if err := r.supervisor.FenceNamespace(closeCtx); err == nil {
					break
				}
				select {
				case <-closeCtx.Done():
					t.Errorf("fence test namespace: %v", closeCtx.Err())
					return
				case <-time.After(10 * time.Millisecond):
				}
			}
			return
		}
		select {
		case <-r.sessionWorkerDone:
			if r.sessionWorkerErr != nil && !config.expectCloseError {
				t.Errorf("Session recovery: %v", r.sessionWorkerErr)
			}
		case <-closeCtx.Done():
			t.Error("Session recovery failed to join")
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := r.waitForInitialSessions(ctx); err != nil {
		t.Fatal(err)
	}
	if err := r.adoptIncarnation(context.Background(), "test-incarnation"); err != nil {
		t.Fatal(err)
	}
	return r
}

func waitRuntime(t *testing.T, r *Runtime, ready func() bool, want string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		if ready() {
			return
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s", want)
		}
	}
}

func modelConfigServer(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	t.Cleanup(server.Close)
	return server
}

func openLifecycleSession(id, cwd string) *environmentruntimepb.CreateSessionRequest {
	return &environmentruntimepb.CreateSessionRequest{SessionId: id, Spec: &sessionpb.SessionSpec{
		Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE,
		}}}, Cwd: cwd, Model: "test-model",
	}}
}

func shellQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'" }

func waitForFile(t *testing.T, path string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s", path)
		}
	}
}

func waitClose(t *testing.T, done <-chan error) error {
	t.Helper()
	select {
	case err := <-done:
		return err
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Runtime close did not complete")
		return nil
	}
}

// Injected controller states are ephemeral; hold a separate real COSI database
// lock so lifecycle tests still exercise the production Runtime ownership barrier.
func newTestRuntime(t *testing.T, config Config, injected state.State, deps runtimeDeps) (*Runtime, error) {
	t.Helper()
	if injected == nil {
		return newRuntime(config, nil, deps)
	}
	if err := retained.EnsureDirectory(config.StateDir); err != nil {
		return nil, err
	}
	_, backing, err := cosistate.Open(config.StateDir, lifecycle.Namespace)
	if err != nil {
		return nil, err
	}
	r, err := newRuntime(config, injected, deps)
	if err != nil {
		_ = backing.Close()
		return nil, err
	}
	r.lifecycleStore = backing
	t.Cleanup(func() { _ = backing.Close() })
	return r, nil
}

func openTestRuntimeStore(dir string) (interface{ Close() error }, error) {
	_, backing, err := cosistate.Open(dir, lifecycle.Namespace)
	return backing, err
}

func closeTestRuntimeStore(r *Runtime) error {
	if r.lifecycleStore == nil {
		return nil
	}
	return r.lifecycleStore.Close()
}
