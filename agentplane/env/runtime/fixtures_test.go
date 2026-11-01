package runtime

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/harness"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/workspace"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
	"google.golang.org/grpc"
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
	host harnesshost.Config
	deps runtimeDeps
}

func lifecycleConfig(t *testing.T, endpoint, workspace string, bootstrap *string, binary string) runtimeTestConfig {
	t.Helper()
	root := t.TempDir()
	launch := configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}, Bootstrap: bootstrap}
	config := Config{StateDir: root, Launch: launch, Supervision: "subprocess", ConfigPath: filepath.Join(root, "config.json"), LaunchPath: filepath.Join(root, "launch.json"), InitializationCwd: workspace, Control: insecureControl(), RuntimeSocket: filepath.Join(root, "runtime.sock"), HostSocket: filepath.Join(root, "host.sock"), ExecutionSocket: filepath.Join(root, "execution.sock")}
	return runtimeTestConfig{Config: config, host: harnesshost.Config{StateDir: root, Launch: launch, WorkspaceRoot: workspace, InitializationCwd: workspace, RuntimeSocket: config.RuntimeSocket, ListenSocket: config.HostSocket, Claude: &harness.LaunchConfig{Binary: binary, BaseURL: endpoint, Token: "test"}, HTTPClient: http.DefaultClient}}
}

func insecureControl() configuration.RunnerControlConfiguration {
	return configuration.RunnerControlConfiguration{ListenAddress: "127.0.0.1:0", Security: runnertransport.Security{Mode: runnertransport.Insecure}}
}

// The fixture composes production Runtime and Host endpoints through real private
// Unix sockets. The Host's private state is never exposed to Runtime assertions.
func newLifecycleRunner(t *testing.T, config runtimeTestConfig) *Runtime {
	t.Helper()
	// Restarts reuse durable state but each fixture owns its own live transports.
	sockets := t.TempDir()
	config.RuntimeSocket = filepath.Join(sockets, "runtime.sock")
	config.HostSocket = filepath.Join(sockets, "host.sock")
	config.ExecutionSocket = filepath.Join(sockets, "execution.sock")
	config.host.RuntimeSocket = config.RuntimeSocket
	config.host.ListenSocket = config.HostSocket
	config.host.ExecutionSocket = config.ExecutionSocket
	r, err := newRuntime(config.Config, nil, config.deps)
	if err != nil {
		t.Fatal(err)
	}
	historyTarget := startAcknowledgingHistory(t)
	private, err := net.Listen("unix", config.RuntimeSocket)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runtimepb.RegisterEnvironmentRuntimeServer(server, r)
	go func() { _ = server.Serve(private) }()
	config.host.StateDir = config.StateDir
	host, err := harnesshost.New(config.host)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	child := &localHostProcess{cancel: cancel, done: make(chan struct{})}
	r.hostProcess = child
	go func() { child.err = host.Run(ctx, configuration.Endpoint{Target: historyTarget}); close(child.done) }()
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer closeCancel()
		if err := r.Close(closeCtx); err != nil {
			t.Errorf("Runtime close: %v", err)
		}
		cancel()
		select {
		case <-child.done:
			if child.err != nil {
				t.Errorf("Host close: %v", child.err)
			}
		case <-closeCtx.Done():
			t.Error("Host failed to join")
		}
		server.Stop()
		_ = private.Close()
	})
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.host != nil && r.host.report != nil && r.host.report.Ready && r.host.report.NativeStopped
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

func openLifecycleSession(id, cwd string) *runnerservicepb.OpenSessionRequest {
	return &runnerservicepb.OpenSessionRequest{SessionId: id, Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model"}}
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

type localHostProcess struct {
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func (p *localHostProcess) Start(context.Context) error { return nil }
func (p *localHostProcess) Wait(ctx context.Context) (int, error) {
	select {
	case <-p.done:
		return 0, p.err
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func (p *localHostProcess) Stop(ctx context.Context) error {
	p.cancel()
	_, err := p.Wait(ctx)
	return err
}
func (p *localHostProcess) Stdout() io.Reader { return strings.NewReader("") }
func (p *localHostProcess) Stderr() io.Reader { return strings.NewReader("") }
