package harnesshost

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// RuntimeBridge exposes Runtime-owned bootstrap and Execution readiness to the
// embedded Host. Its callbacks are made without holding the Host mutex.
type RuntimeBridge interface {
	WaitHostBootstrap(context.Context, *runtimepb.Binding) error
	HostExecutionReady(context.Context) (bool, error)
	HostFailed(error)
}

type bindingContextKey struct{}

func withBinding(ctx context.Context, binding *runtimepb.Binding) context.Context {
	return context.WithValue(ctx, bindingContextKey{}, proto.Clone(binding).(*runtimepb.Binding))
}

type runtimeBinding struct {
	runtimeBootID string
	active        bool
	failed        bool
}

// Runtime revokes this native binding gate synchronously, even if COSI is
// unavailable. Intent application still comes only from the shared COSI store.
func (r *Host) SetRuntimeBinding(runtimeBootID string, active bool) {
	for {
		current := r.runtimeBinding.Load()
		if current != nil && current.failed {
			return
		}
		if r.runtimeBinding.CompareAndSwap(current, &runtimeBinding{runtimeBootID: runtimeBootID, active: active}) {
			return
		}
	}
}

// RuntimeBindingMatches checks the native gate used by Host intent admission.
func (r *Host) RuntimeBindingMatches(runtimeBootID string) bool {
	binding := r.runtimeBinding.Load()
	return binding != nil && binding.active && !binding.failed && binding.runtimeBootID == runtimeBootID
}

// BoundContext checks the exact lifecycle binding at direct-call ingress. Native
// admission repeats this check under the Host mutex before any durable mutation.
func (r *Host) BoundContext(ctx context.Context, binding *runtimepb.Binding) (context.Context, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.authorizeBindingLocked(binding); err != nil {
		return nil, err
	}
	return withBinding(ctx, binding), nil
}

func (r *Host) environment() session.Environment {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.intent == nil {
		return session.Environment{Quiesced: true}
	}
	return session.Environment{IncarnationUID: r.intent.IncarnationUid, Quiesced: r.intent.Mode != runtimepb.LifecycleIntent_RUNNING}
}

// Caller holds the synchronous native admission barrier. The interceptor alone
// cannot fence a request that spent time waiting on model lookup or creation.
func (r *Host) authorizeLocked(ctx context.Context) error {
	binding, _ := ctx.Value(bindingContextKey{}).(*runtimepb.Binding)
	return r.authorizeBindingLocked(binding)
}

func (r *Host) authorizeBindingLocked(binding *runtimepb.Binding) error {
	if r.closing || !r.ready.Load() || r.intent == nil {
		return status.Error(codes.Unavailable, "Harness Host has no ready environment binding")
	}
	if binding == nil {
		return status.Error(codes.Unauthenticated, "environment binding is required")
	}
	if !proto.Equal(binding, r.intent.Binding) {
		return status.Error(codes.FailedPrecondition, "stale environment binding")
	}
	if !r.RuntimeBindingMatches(binding.RuntimeBootId) {
		return status.Error(codes.Unavailable, "Harness Host binding is inactive")
	}
	return nil
}

func (r *Host) ApplyIntent(ctx context.Context, intent *runtimepb.LifecycleIntent) error {
	if intent == nil || intent.Binding == nil || intent.Binding.RuntimeBootId == "" || intent.Binding.Generation == 0 || intent.SandboxUid != r.config.Launch.Sandbox.UID {
		return errors.New("invalid environment lifecycle binding")
	}
	mode := harnesshostlifecycle.Quiescing
	switch intent.Mode {
	case runtimepb.LifecycleIntent_INACTIVE:
		if intent.IncarnationUid != "" || intent.SealUid != "" {
			return errors.New("inactive environment carries an incarnation")
		}
	case runtimepb.LifecycleIntent_RUNNING:
		if intent.IncarnationUid == "" || intent.SealUid != "" {
			return errors.New("running environment has invalid incarnation or seal")
		}
		mode = harnesshostlifecycle.Running
	case runtimepb.LifecycleIntent_QUIESCING:
		if intent.IncarnationUid == "" || intent.SealUid != intent.IncarnationUid {
			return errors.New("draining environment has no incarnation")
		}
	case runtimepb.LifecycleIntent_CLOSING:
		if intent.SealUid != "" && intent.SealUid != intent.IncarnationUid {
			return errors.New("closing environment seal does not match incarnation")
		}
	default:
		return errors.New("unknown environment lifecycle mode")
	}
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return errors.New("Harness Host is closing")
	}
	previous := r.intent
	if previous != nil {
		if intent.Binding.RuntimeBootId != previous.Binding.RuntimeBootId || intent.Binding.Generation < previous.Binding.Generation {
			r.mu.Unlock()
			return errors.New("environment lifecycle binding regressed")
		}
		if intent.Binding.Generation == previous.Binding.Generation {
			old := proto.Clone(previous).(*runtimepb.LifecycleIntent)
			old.ExecutionFenced = intent.ExecutionFenced
			if !proto.Equal(old, intent) || previous.ExecutionFenced && !intent.ExecutionFenced {
				r.mu.Unlock()
				return errors.New("environment lifecycle intent changed without a new generation")
			}
		}
	}
	changedGeneration := previous == nil || intent.Binding.Generation != previous.Binding.Generation
	if changedGeneration && previous != nil && (previous.Mode != runtimepb.LifecycleIntent_RUNNING || previous.IncarnationUid != intent.IncarnationUid) && intent.Mode == runtimepb.LifecycleIntent_RUNNING && !r.nativeStopped {
		r.mu.Unlock()
		return errors.New("environment reopened before native drain completed")
	}
	r.ready.Store(false)
	r.intent = proto.Clone(intent).(*runtimepb.LifecycleIntent)
	if mode == harnesshostlifecycle.Running {
		r.bootstrapBinding = proto.Clone(intent.Binding).(*runtimepb.Binding)
	}
	r.mu.Unlock()
	if changedGeneration && mode == harnesshostlifecycle.Running {
		if err := r.sessions.ResetDemand(); err != nil {
			return err
		}
		if r.codexAppServer != nil {
			if err := r.codexAppServer.ResetDemand(ctx); err != nil {
				return err
			}
		}
		for _, s := range r.sessions.Inventory() {
			if err := r.sessions.Reopen(ctx, s); err != nil {
				return err
			}
			if err := r.sessions.ResumeRecovered(s); err != nil {
				return err
			}
		}
	}
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return errors.New("Harness Host is closing")
	}
	if changedGeneration {
		r.nativeStopped = false
	}
	err := harnesshostlifecycle.PublishDesired(r.ctx, r.lifecycle.state, harnesshostlifecycle.Desired{Mode: mode, SealUID: intent.SealUid, Generation: intent.Binding.Generation})
	if err == nil {
		r.ready.Store(true)
	}
	r.mu.Unlock()
	r.changed()
	return err
}

func (r *Host) waitBootstrap(ctx context.Context) error {
	r.mu.Lock()
	if r.bootstrapBinding == nil || r.runtimeBridge == nil {
		r.mu.Unlock()
		return errors.New("environment runtime binding is unavailable")
	}
	binding := proto.Clone(r.bootstrapBinding).(*runtimepb.Binding)
	bridge := r.runtimeBridge
	r.mu.Unlock()
	return bridge.WaitHostBootstrap(ctx, binding)
}

func (r *Host) executionReady(ctx context.Context) (bool, error) {
	r.mu.Lock()
	bridge := r.runtimeBridge
	r.mu.Unlock()
	if bridge == nil {
		return r.config.ExecServerURL == "", nil
	}
	return bridge.HostExecutionReady(ctx)
}
