package runtime

import (
	"context"
	"sync"
	"testing"

	execution "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
)

type localExecutionHandle struct {
	adapter  *execution.Adapter
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

func (h *localExecutionHandle) exitWithoutFence(code int) {
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
		adapter, err := execution.New(execution.Config{Context: context.Background(), Admit: func(ctx context.Context, _ *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
			return accept(ctx)
		}, Acquire: acquire})
		if err != nil {
			return nil, err
		}
		return &localExecutionHandle{adapter: adapter}, nil
	}
	return config
}
