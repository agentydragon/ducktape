package runtime

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/workspace"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

const lifecycleTestTimeout = 10 * time.Second

func TestMain(m *testing.M) {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case supervision.SupervisorMode:
			os.Exit(supervision.SupervisorMain(os.Args[2:]))
		case workspace.HelperMode:
			os.Exit(workspace.Main(os.Args[2:]))
		}
	}
	os.Exit(m.Run())
}

type runtimeTestConfig struct {
	Config
	host             harnesshost.Config
	deps             runtimeDeps
	expectCloseError bool
}

func lifecycleConfig(t *testing.T, endpoint, workspace string, bootstrap *string, binary string) runtimeTestConfig {
	t.Helper()
	root := t.TempDir()
	launch := configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}, Bootstrap: bootstrap}
	config := Config{StateDir: root, Launch: launch, Supervision: "subprocess", InitializationCwd: workspace, Control: insecureControl()}
	return runtimeTestConfig{Config: config, host: harnesshost.Config{StateDir: root, Launch: launch, WorkspaceRoot: workspace, InitializationCwd: workspace, Claude: &harness.LaunchConfig{Binary: binary, BaseURL: endpoint, Token: "test"}, HTTPClient: http.DefaultClient}}
}

func insecureControl() configuration.EnvironmentRuntimeControlConfiguration {
	return configuration.EnvironmentRuntimeControlConfiguration{ListenAddress: "127.0.0.1:0", Security: environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}}
}

// The fixture composes production Runtime and Host through their direct coordinator.
func newLifecycleRuntime(t *testing.T, config runtimeTestConfig) *Runtime {
	t.Helper()
	r, err := newRuntime(config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	historyTarget := startAcknowledgingHistory(t)
	config.host.StateDir = config.StateDir
	host, err := harnesshost.NewEmbedded(config.host, r.lifecycleState, r.lifecycleEngine, r.lifecycleDone, r.processes)
	if err != nil {
		t.Fatal(err)
	}
	r.startLifecycleEngine()
	ctx, cancel := context.WithCancel(context.Background())
	child := &localHostLifetime{host: host, cancel: cancel, done: make(chan struct{})}
	r.hostLifetime = child
	go func() { child.err = host.Run(ctx, configuration.Endpoint{Target: historyTarget}, r); close(child.done) }()
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer closeCancel()
		closeErr := r.Close(closeCtx)
		if (closeErr != nil) != config.expectCloseError {
			t.Errorf("Runtime close error=%v, expected error=%v", closeErr, config.expectCloseError)
		}
		if config.expectCloseError && closeErr != nil {
			// An unproved fence deliberately retains the shared engine and owner.
			// This fixture owns only temporary state, so stop the engine to join
			// the remaining Host goroutines after the test's assertions.
			r.lifecycleCancel()
			<-r.lifecycleDone
		}
		cancel()
		select {
		case <-child.done:
			if child.err != nil && !config.expectCloseError {
				t.Errorf("Host close: %v", child.err)
			}
		case <-closeCtx.Done():
			t.Error("Host failed to join")
		}
	})
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.hostReportLocked() != nil && r.hostReportLocked().Ready && r.hostReportLocked().NativeStopped
	}, "Host initial binding")
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

func openLifecycleSession(id, cwd string) *environmentruntimepb.OpenSessionRequest {
	return &environmentruntimepb.OpenSessionRequest{SessionId: id, Spec: &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model"}}
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

type localHostLifetime struct {
	host   *harnesshost.Host
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func (p *localHostLifetime) Wait(ctx context.Context) (int, error) {
	select {
	case <-p.done:
		return 0, p.err
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (p *localHostLifetime) Stop(ctx context.Context) error {
	p.cancel()
	return ctx.Err()
}

func (p *localHostLifetime) WaitClosed(ctx context.Context) error { return p.host.WaitClosed(ctx) }

func (p *localHostLifetime) Cleanup(ctx context.Context) error { return p.host.Close(ctx) }
