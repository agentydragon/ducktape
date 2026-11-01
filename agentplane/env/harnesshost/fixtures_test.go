package harnesshost

import (
	"context"
	"time"

	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Native tests hold bootstrap complete; bootstrap process ownership is exercised
// by Runtime tests. Calls still carry the same binding as the private transport.
type completedBootstrapClient struct {
	runtimepb.EnvironmentRuntimeClient
}

func (completedBootstrapClient) WaitBootstrap(ctx context.Context, _ *runtimepb.Binding, _ ...grpc.CallOption) (*emptypb.Empty, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return &emptypb.Empty{}, nil
}

func bindHost(r *Runner, incarnation string) error {
	r.mu.Lock()
	r.runtimeClient = completedBootstrapClient{}
	generation := uint64(1)
	if r.intent != nil {
		generation = r.intent.Binding.Generation + 1
	}
	r.mu.Unlock()
	return r.applyIntent(r.ctx, &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "test-runtime", HostBootId: r.bootID, Generation: generation}, SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnation, Mode: runtimepb.LifecycleIntent_RUNNING})
}

func hostContext(r *Runner, ctx context.Context) context.Context {
	r.mu.Lock()
	defer r.mu.Unlock()
	return bindingContext(ctx, r.intent.Binding)
}

func quiesceHost(ctx context.Context, r *Runner) (*runnerpb.QuiesceResponse, error) {
	r.mu.Lock()
	intent := proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	r.mu.Unlock()
	intent.Binding.Generation++
	intent.Mode = runtimepb.LifecycleIntent_QUIESCING
	intent.SealUid = intent.IncarnationUid
	if err := r.applyIntent(ctx, intent); err != nil {
		return nil, err
	}
	// This fixture has no Execution process. Publish its fence only after Host has
	// actually drained native work, just as the Runtime control protocol requires.
	go func() {
		ctx, cancel := context.WithTimeout(r.ctx, 10*time.Second)
		defer cancel()
		if err := r.lifecycle.waitFor(ctx, func() (bool, error) { r.mu.Lock(); defer r.mu.Unlock(); return r.nativeStopped || r.closing, nil }); err != nil {
			return
		}
		r.mu.Lock()
		closing := r.closing || r.intent.Binding.Generation != intent.Binding.Generation
		r.mu.Unlock()
		if closing {
			return
		}
		intent.ExecutionFenced = true
		if err := r.applyIntent(ctx, intent); err != nil {
			r.fail(err)
		}
	}()
	return &runnerpb.QuiesceResponse{}, nil
}
