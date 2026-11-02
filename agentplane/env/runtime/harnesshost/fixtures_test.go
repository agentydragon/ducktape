package harnesshost

import (
	"context"
	"time"

	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"
)

// Native tests hold bootstrap complete; bootstrap process ownership is exercised
// by Runtime tests. Calls carry the exact in-process binding.
type completedBootstrapBridge struct{}

func (completedBootstrapBridge) ConnectHost(ctx context.Context, _ *Host) error { return ctx.Err() }

func (completedBootstrapBridge) WaitHostBootstrap(ctx context.Context, _ *runtimepb.Binding) error {
	return ctx.Err()
}

func (completedBootstrapBridge) HostExecutionReady(context.Context) (bool, error) { return true, nil }

func bindHost(r *Host, incarnation string) error {
	r.mu.Lock()
	r.runtimeBridge = completedBootstrapBridge{}
	generation := uint64(1)
	if r.intent != nil {
		generation = r.intent.Binding.Generation + 1
	}
	r.mu.Unlock()
	return r.ApplyIntent(r.ctx, &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "test-runtime", HostBootId: r.bootID, Generation: generation}, SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnation, Mode: runtimepb.LifecycleIntent_RUNNING})
}

func hostContext(r *Host, ctx context.Context) context.Context {
	r.mu.Lock()
	defer r.mu.Unlock()
	return withBinding(ctx, r.intent.Binding)
}

func quiesceHost(ctx context.Context, r *Host) (*sessionpb.QuiesceResponse, error) {
	r.mu.Lock()
	intent := proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	r.mu.Unlock()
	intent.Binding.Generation++
	intent.Mode = runtimepb.LifecycleIntent_QUIESCING
	intent.SealUid = intent.IncarnationUid
	if err := r.ApplyIntent(ctx, intent); err != nil {
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
		if err := r.ApplyIntent(ctx, intent); err != nil {
			r.fail(err)
		} else if err := publishHostIntentStatus(ctx, r, intent, true); err != nil {
			r.fail(err)
		}
	}()
	return &sessionpb.QuiesceResponse{}, nil
}
