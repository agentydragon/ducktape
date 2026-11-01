package runtime

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type hostLifecyclePeer interface {
	BootID() string
	ApplyIntent(context.Context, *runtimepb.LifecycleIntent) error
	Report() (*runtimepb.HostReport, error)
	ChangesSince() <-chan struct{}
}

// ConnectHost is the sole in-process lifecycle coordinator. It serializes intent
// application and report observation without holding either component mutex
// across a call into the other component.
func (r *Runtime) ConnectHost(ctx context.Context, client *harnesshost.Runner) error {
	return r.connectHost(ctx, client, client)
}

func (r *Runtime) connectHost(ctx context.Context, client *harnesshost.Runner, peer hostLifecyclePeer) (resultErr error) {
	bootID := peer.BootID()
	if bootID == "" {
		return status.Error(codes.InvalidArgument, "Harness Host has no boot ID")
	}
	host := &hostLink{bootID: bootID, client: client, intentChanged: make(chan struct{}), connected: true}
	r.mu.Lock()
	if r.closing || r.failed {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "environment Runtime is closing or failed")
	}
	if r.host != nil && r.host.connected {
		r.mu.Unlock()
		return status.Error(codes.AlreadyExists, "another Harness Host is already connected")
	}
	r.host = host
	if r.intent == nil {
		r.intent = &runtimepb.LifecycleIntent{
			Binding:    &runtimepb.Binding{RuntimeBootId: r.bootID, HostBootId: bootID, Generation: 1},
			SandboxUid: r.config.Launch.Sandbox.UID, Mode: runtimepb.LifecycleIntent_INACTIVE,
		}
	} else {
		r.intent = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
		r.intent.Binding.HostBootId = bootID
		r.intent.Binding.Generation++
	}
	r.changedLocked()
	r.mu.Unlock()
	defer func() { r.disconnectHost(host, resultErr) }()

	bridgeCtx, cancel := context.WithCancel(ctx)
	var workers sync.WaitGroup
	intentResults := make(chan error, 1)
	intentApplied := make(chan struct{}, 1)
	workers.Go(func() { intentResults <- r.applyHostIntents(bridgeCtx, host, peer, intentApplied) })
	defer func() { cancel(); workers.Wait() }()

	var lastReport *runtimepb.HostReport
	for {
		hostChanged := peer.ChangesSince()
		report, err := peer.Report()
		if err != nil {
			return fmt.Errorf("observe Host lifecycle report: %w", err)
		}
		if report != nil && !proto.Equal(report, lastReport) {
			if err := r.acceptReport(host, report); err != nil {
				return err
			}
			lastReport = proto.Clone(report).(*runtimepb.HostReport)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-intentResults:
			return err
		case <-intentApplied:
			// Applying an intent can clear Runtime's report without changing
			// any Host report fields (notably the same-generation fence).
			// Re-observe even when Host's value is byte-identical.
			lastReport = nil
		case <-hostChanged:
		}
	}
}

// Intent application can wait for accepted Session setup. Observe reports on
// the other worker so that wait cannot prevent a drain acknowledgement. The
// single intent worker still applies generations in order.
func (r *Runtime) applyHostIntents(ctx context.Context, host *hostLink, peer hostLifecyclePeer, applied chan<- struct{}) error {
	var lastIntent *runtimepb.LifecycleIntent
	for {
		r.mu.Lock()
		changed := host.intentChanged
		var current *runtimepb.LifecycleIntent
		if r.intent != nil {
			current = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
		}
		r.mu.Unlock()
		if current != nil && !proto.Equal(current, lastIntent) {
			if err := peer.ApplyIntent(ctx, current); err != nil {
				return fmt.Errorf("apply Host lifecycle intent: %w", err)
			}
			lastIntent = current
			r.mu.Lock()
			if r.host == host && host.connected {
				host.intent = proto.Clone(current).(*runtimepb.LifecycleIntent)
				r.changedLocked()
			}
			r.mu.Unlock()
			select {
			case applied <- struct{}{}:
			default:
			}
			continue
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

func (r *Runtime) acceptReport(host *hostLink, report *runtimepb.HostReport) error {
	if report == nil || report.Binding == nil || report.Facts == nil {
		return status.Error(codes.InvalidArgument, "HostReport requires a binding and EnvironmentRuntimeFacts")
	}
	r.mu.Lock()
	if r.host != host || !host.connected {
		r.mu.Unlock()
		return status.Error(codes.Aborted, "HostReport belongs to a superseded Host connection")
	}
	if !proto.Equal(report.Binding, r.intent.GetBinding()) {
		// A report can be in flight while the Host applies a newer intent. It is never
		// an acknowledgement for that new generation; drop it and wait for a bound report.
		r.mu.Unlock()
		return nil
	}
	if report.Error != "" {
		r.mu.Unlock()
		return status.Errorf(codes.FailedPrecondition, "Harness Host rejected lifecycle generation %d: %s", report.Binding.Generation, report.Error)
	}
	host.report = proto.Clone(report).(*runtimepb.HostReport)
	var fenceBinding *runtimepb.Binding
	if report.Ready && report.NativeStopped && r.intent != nil && !r.intent.ExecutionFenced &&
		(r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING || r.intent.Mode == runtimepb.LifecycleIntent_CLOSING) {
		fenceBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	}
	r.changedLocked()
	r.mu.Unlock()
	if fenceBinding != nil {
		r.maybeFenceExecution(fenceBinding)
	}
	return nil
}

func (r *Runtime) maybeFenceExecution(binding *runtimepb.Binding) {
	r.mu.Lock()
	if r.host == nil || !r.host.connected || r.intent == nil || !proto.Equal(r.intent.Binding, binding) ||
		r.intent.ExecutionFenced || r.host.report == nil || !r.host.report.Ready || !r.host.report.NativeStopped ||
		!proto.Equal(r.host.report.Binding, binding) || (r.intent.Mode != runtimepb.LifecycleIntent_QUIESCING && r.intent.Mode != runtimepb.LifecycleIntent_CLOSING) {
		r.mu.Unlock()
		return
	}
	if r.fenceInFlight != nil && proto.Equal(r.fenceInFlight, binding) {
		r.mu.Unlock()
		return
	}
	r.fenceInFlight = proto.Clone(binding).(*runtimepb.Binding)
	r.mu.Unlock()

	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		// Retain the in-flight marker on failure: an uncertain stop or fence
		// publication must not be attempted again for this binding.
		r.cancelExecutionCalls()
		if err := r.waitForExecutionCalls(ctx); err != nil {
			r.fail(fmt.Errorf("drain forwarded Execution calls before fencing: %w", err))
			return
		}
		if err := r.stopExecution(ctx); err != nil {
			r.fail(fmt.Errorf("fence embedded Execution API: %w", err))
			return
		}
		if err := r.setExecutionFenced(binding); err != nil {
			r.fail(fmt.Errorf("publish embedded Execution fence: %w", err))
			return
		}
		r.mu.Lock()
		if proto.Equal(r.fenceInFlight, binding) {
			r.fenceInFlight = nil
		}
		r.mu.Unlock()
	}()
}

func (r *Runtime) cancelExecutionCalls() {
	r.mu.Lock()
	cancels := make([]context.CancelFunc, 0, len(r.execCalls)+len(r.execStreams))
	for _, cancel := range r.execCalls {
		cancels = append(cancels, cancel)
	}
	for _, cancel := range r.execStreams {
		cancels = append(cancels, cancel)
	}
	r.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
}

func (r *Runtime) waitForExecutionCalls(ctx context.Context) error {
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil && !r.closing {
			r.mu.Unlock()
			return failure
		}
		if r.activeExecutionCalls == 0 {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (r *Runtime) disconnectHost(host *hostLink, cause error) {
	r.mu.Lock()
	if r.host == host && host.connected {
		host.connected = false
		r.changedLocked()
		closing := r.closing
		r.mu.Unlock()
		if !closing && cause != nil && !errors.Is(cause, context.Canceled) {
			r.fail(fmt.Errorf("Harness Host lifecycle connection lost before fence acknowledgement: %w", cause))
		}
		return
	}
	r.mu.Unlock()
}

func (r *Runtime) WaitHostBootstrap(ctx context.Context, binding *runtimepb.Binding) error {
	if err := ctx.Err(); err != nil {
		return status.FromContextError(err).Err()
	}
	r.mu.Lock()
	valid := binding != nil && r.host != nil && r.host.connected && r.lastRunning != nil &&
		proto.Equal(binding, r.lastRunning) && binding.RuntimeBootId == r.bootID && binding.HostBootId == r.host.bootID
	r.mu.Unlock()
	if !valid {
		return status.Error(codes.FailedPrecondition, "bootstrap wait is outside the admitted environment binding")
	}
	// Runtime admits the one-shot bootstrap during incarnation adoption. Host receives only
	// this read/wait boundary; canceling this RPC never cancels the Runtime-owned operation.
	var wait func(context.Context) error
	if r.waitBootstrap != nil {
		wait = r.waitBootstrap
	} else if r.bootstrapHost != nil {
		wait = r.bootstrapHost.Wait
	}
	if wait == nil {
		return nil
	}
	if err := wait(ctx); err != nil {
		return err
	}
	return nil
}

func (r *Runtime) HostExecutionReady(ctx context.Context) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.config.CodexExecServer == nil {
		return true, nil
	}
	if service, ok := r.executionOwner.(*executionServiceHandle); ok && !service.serving() {
		return false, nil
	}
	if stopping, ok := r.executionOwner.(interface{ stopRequested() bool }); ok && stopping.stopRequested() {
		return false, nil
	}
	return !r.closing && !r.executionRecovering && r.executionOwner != nil && r.executionServer != nil &&
		r.intent != nil && r.executionUID == r.intent.IncarnationUid, nil
}

func (r *Runtime) setIntentLocked(mode runtimepb.LifecycleIntent_Mode, incarnationUID, sealUID string, executionFenced bool) *runtimepb.LifecycleIntent {
	if r.host == nil {
		return nil
	}
	var generation uint64 = 1
	if r.intent != nil && r.intent.Binding != nil {
		generation = r.intent.Binding.Generation + 1
	}
	r.intent = &runtimepb.LifecycleIntent{
		Binding:    &runtimepb.Binding{RuntimeBootId: r.bootID, HostBootId: r.host.bootID, Generation: generation},
		SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnationUID,
		Mode: mode, SealUid: sealUID, ExecutionFenced: executionFenced,
	}
	r.host.report = nil
	r.intentChangedLocked()
	return proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
}

func (r *Runtime) setExecutionFenced(binding *runtimepb.Binding) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.intent == nil || !proto.Equal(r.intent.Binding, binding) {
		return status.Error(codes.Aborted, "execution fence belongs to a stale environment generation")
	}
	if r.intent.ExecutionFenced {
		return nil
	}
	if r.host == nil || !r.host.connected {
		return status.Error(codes.Unavailable, "Harness Host connection was lost before publishing Execution fence")
	}
	r.intent.ExecutionFenced = true
	r.host.report = nil
	r.intentChangedLocked()
	return nil
}

func (r *Runtime) waitForExecutionFenced(ctx context.Context, binding *runtimepb.Binding) error {
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		if r.host == nil || !r.host.connected {
			r.mu.Unlock()
			return status.Error(codes.Unavailable, "Harness Host connection was lost before Execution fence")
		}
		// Publishing the fence locally is not enough: stopping Host before it
		// applies this update can make Close reject the delayed intent.
		if r.intent != nil && proto.Equal(r.intent.Binding, binding) && r.intent.ExecutionFenced &&
			r.host.intent != nil && proto.Equal(r.host.intent.Binding, binding) && r.host.intent.ExecutionFenced {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (r *Runtime) waitForHostReady(ctx context.Context, binding *runtimepb.Binding) (*runtimepb.HostReport, error) {
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return nil, failure
		}
		if r.host == nil || !r.host.connected {
			r.mu.Unlock()
			return nil, status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
		}
		if r.host.report != nil && r.host.report.Ready && proto.Equal(r.host.report.Binding, binding) {
			report := proto.Clone(r.host.report).(*runtimepb.HostReport)
			r.mu.Unlock()
			return report, nil
		}
		changed := r.changes
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

func (r *Runtime) waitForHostQuiesced(ctx context.Context, binding *runtimepb.Binding) error {
	for {
		quiesced, changed, err := r.hostQuiescenceWaitSnapshot(binding)
		if err != nil {
			return err
		}
		if quiesced {
			return nil
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (r *Runtime) hostQuiescenceWaitSnapshot(binding *runtimepb.Binding) (bool, <-chan struct{}, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.host != nil && r.host.connected && r.host.report != nil && r.host.report.Ready &&
		proto.Equal(r.host.report.Binding, binding) &&
		r.host.report.Facts.GetQuiesceStage() == environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED {
		return true, nil, nil
	}
	if failure := r.failureLocked(); failure != nil {
		return false, nil, failure
	}
	if r.host == nil || !r.host.connected {
		return false, nil, status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
	}
	return false, r.changes, nil
}
