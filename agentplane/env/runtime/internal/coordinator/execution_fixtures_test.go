package coordinator

import (
	"context"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedproxy"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type localExecutionHandle struct {
	adapter  *typedproxy.Proxy
	mu       sync.Mutex
	exited   chan childExit
	expected bool
}

func (h *localExecutionHandle) Executor() executorpb.ExecutorServer { return h.adapter }
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
	if h.exited != nil {
		h.exited <- childExit{code: 0}
	}
	return nil
}
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
	h.exited <- childExit{code: 0}
}

func newExecutionRuntime(t *testing.T, url string, acquire func(context.Context) (string, error)) *Runtime {
	t.Helper()
	return newLifecycleRuntime(t, executionRuntimeConfig(t, url, acquire))
}

func executionRuntimeConfig(t *testing.T, url string, acquire func(context.Context) (string, error)) runtimeTestConfig {
	t.Helper()
	config := lifecycleConfig(t, "http://unused", t.TempDir(), nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: url}
	config.HarnessEnvironment = map[string]string{}

	config.deps.startExecution = func(_ context.Context, config Config, _ string) (executionHandle, error) {
		target := url
		if target == "ws://unused" {
			target = "127.0.0.1:1"
		}
		conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return nil, err
		}
		t.Cleanup(func() { _ = conn.Close() })
		client := executorpb.NewExecutorClient(conn)
		if acquire != nil {
			client = &delayedExecutorClient{ExecutorClient: client, acquire: acquire}
		}
		localAdapter, err := typedproxy.New(context.Background(), client, func(ctx context.Context, _ *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
			return accept(ctx)
		}, nil)
		if err != nil {
			return nil, err
		}
		return &localExecutionHandle{adapter: localAdapter}, nil
	}
	return config
}

// delayedExecutorClient keeps the fixture's blocked-open admission seam.
type delayedExecutorClient struct {
	executorpb.ExecutorClient
	acquire func(context.Context) (string, error)
}

func (c *delayedExecutorClient) OpenSession(ctx context.Context, r *executorpb.OpenSessionRequest, opts ...grpc.CallOption) (executorpb.Executor_OpenSessionClient, error) {
	if _, err := c.acquire(ctx); err != nil {
		return nil, err
	}
	return c.ExecutorClient.OpenSession(ctx, r, opts...)
}
