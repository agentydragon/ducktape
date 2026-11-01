package runtime

import (
	"context"
	"net"
	"sync"
	"testing"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	execution "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
)

type localExecutionHandle struct {
	conn     *grpc.ClientConn
	server   *grpc.Server
	adapter  *execution.Adapter
	listener net.Listener
	mu       sync.Mutex
	exited   chan childExit
	expected bool
}

func (h *localExecutionHandle) ClientConn() *grpc.ClientConn { return h.conn }
func (h *localExecutionHandle) StopAndFence(ctx context.Context) error {
	h.mu.Lock()
	h.expected = true
	h.mu.Unlock()
	if err := h.adapter.Drain(ctx); err != nil {
		return err
	}
	if err := h.adapter.Close(); err != nil {
		return err
	}
	h.server.Stop()
	if h.exited != nil {
		h.exited <- childExit{code: 0}
	}
	return nil
}
func (h *localExecutionHandle) Close() error { return h.conn.Close() }

func (h *localExecutionHandle) ProcessExit() <-chan childExit { return h.exited }

func (h *localExecutionHandle) stopRequested() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.expected
}

func (h *localExecutionHandle) loseBackendAfterFence() {
	if err := h.adapter.Close(); err != nil {
		panic(err)
	}
	h.server.Stop()
	h.exited <- childExit{code: 0}
}

func (h *localExecutionHandle) exitWithoutFence(code int) {
	if h.server != nil {
		h.server.Stop()
	}
	h.exited <- childExit{code: code}
}

func newExecutionRuntime(t *testing.T, url string, acquire func(context.Context) (string, error)) *Runtime {
	t.Helper()
	return newLifecycleRunner(t, executionRuntimeConfig(t, url, acquire))
}

func executionRuntimeConfig(t *testing.T, url string, acquire func(context.Context) (string, error)) runtimeTestConfig {
	t.Helper()
	config := lifecycleConfig(t, "http://unused", t.TempDir(), nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: url}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	if acquire == nil {
		acquire = func(context.Context) (string, error) { return url, nil }
	}
	config.deps.startExecution = func(_ context.Context, config Config, _ string) (executionHandle, error) {
		path := config.ExecutionSocket
		listener, err := net.Listen("unix", path)
		if err != nil {
			return nil, err
		}
		adapter, err := execution.New(execution.Config{Context: context.Background(), Admit: func(ctx context.Context, _ *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
			return accept(ctx)
		}, Acquire: acquire})
		if err != nil {
			_ = listener.Close()
			return nil, err
		}
		server := grpc.NewServer()
		executorpb.RegisterExecutorServer(server, adapter)
		healthServer := health.NewServer()
		healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
		healthServer.SetServingStatus(executorpb.Executor_ServiceDesc.ServiceName, grpc_health_v1.HealthCheckResponse_SERVING)
		grpc_health_v1.RegisterHealthServer(server, healthServer)
		go func() { _ = server.Serve(listener) }()
		conn, err := grpc.NewClient("unix://"+path, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			server.Stop()
			_ = adapter.Close()
			return nil, err
		}
		return &localExecutionHandle{conn: conn, server: server, adapter: adapter, listener: listener}, nil
	}
	return config
}
