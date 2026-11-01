package runtime

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

func (r *Runtime) Connect(stream runtimepb.EnvironmentRuntime_ConnectServer) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	hello := first.GetHello()
	if hello == nil || hello.HostBootId == "" || hello.ControlSocket != r.config.HostSocket || !filepath.IsAbs(hello.ControlSocket) {
		return status.Error(codes.InvalidArgument, "first lifecycle message must be a valid HostHello for the configured control socket")
	}
	conn, err := r.deps.dialHost(hello.ControlSocket)
	if err != nil {
		return status.Errorf(codes.Unavailable, "connect private Harness Host socket: %v", err)
	}
	host := &hostLink{
		bootID: hello.HostBootId, controlSocket: hello.ControlSocket, conn: conn,
		client: environmentruntimepb.NewEnvironmentRuntimeClient(conn), intentChanged: make(chan struct{}), connected: true,
	}
	r.mu.Lock()
	if r.closing || r.failed {
		r.mu.Unlock()
		_ = conn.Close()
		return status.Error(codes.Unavailable, "environment Runtime is closing or failed")
	}
	if r.host != nil && r.host.connected {
		r.mu.Unlock()
		_ = conn.Close()
		return status.Error(codes.AlreadyExists, "another Harness Host is already connected")
	}
	r.host = host
	if r.intent == nil {
		r.intent = &runtimepb.LifecycleIntent{
			Binding:    &runtimepb.Binding{RuntimeBootId: r.bootID, HostBootId: hello.HostBootId, Generation: 1},
			SandboxUid: r.config.Launch.Sandbox.UID, Mode: runtimepb.LifecycleIntent_INACTIVE,
		}
	} else {
		r.intent = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
		r.intent.Binding.HostBootId = hello.HostBootId
		r.intent.Binding.Generation++
	}
	host.intent = proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	initialIntent := proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	r.changedLocked()
	r.mu.Unlock()
	if err := stream.Send(initialIntent); err != nil {
		r.disconnectHost(host, err)
		return err
	}

	reports := make(chan *runtimepb.HostReport, 1)
	readErrors := make(chan error, 1)
	go func() {
		for {
			request, err := stream.Recv()
			if err != nil {
				readErrors <- err
				return
			}
			report := request.GetReport()
			if report == nil {
				readErrors <- status.Error(codes.InvalidArgument, "only HostReport messages may follow HostHello")
				return
			}
			select {
			case reports <- report:
			case <-stream.Context().Done():
				return
			}
		}
	}()
	for {
		r.mu.Lock()
		changed := host.intentChanged
		current := r.intent
		if current != nil {
			current = proto.Clone(current).(*runtimepb.LifecycleIntent)
		}
		lastSent := host.intent
		r.mu.Unlock()
		if current != nil && !proto.Equal(current, lastSent) {
			if err := stream.Send(current); err != nil {
				r.disconnectHost(host, err)
				return err
			}
			r.mu.Lock()
			if r.host == host && host.connected {
				host.intent = current
			}
			r.mu.Unlock()
			continue
		}
		select {
		case <-stream.Context().Done():
			r.disconnectHost(host, stream.Context().Err())
			return stream.Context().Err()
		case err := <-readErrors:
			r.disconnectHost(host, err)
			if stream.Context().Err() != nil {
				return stream.Context().Err()
			}
			return status.Errorf(codes.Unavailable, "Harness Host lifecycle stream ended: %v", err)
		case report := <-reports:
			if err := r.acceptReport(host, report); err != nil {
				r.disconnectHost(host, err)
				return err
			}
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
		r.cancelExecutionCalls()
		if err := r.waitForExecutionCalls(ctx); err != nil {
			r.mu.Lock()
			if proto.Equal(r.fenceInFlight, binding) {
				r.fenceInFlight = nil
			}
			r.mu.Unlock()
			r.fail(fmt.Errorf("drain forwarded Execution calls before fencing: %w", err))
			return
		}
		if err := r.stopExecution(ctx); err != nil {
			r.mu.Lock()
			if proto.Equal(r.fenceInFlight, binding) {
				r.fenceInFlight = nil
			}
			r.mu.Unlock()
			r.fail(fmt.Errorf("fence embedded Execution API: %w", err))
			return
		}
		if err := r.setExecutionFenced(binding); err != nil {
			r.fail(fmt.Errorf("publish embedded Execution fence: %w", err))
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
		if failure := r.failureLocked(); failure != nil {
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
		if host.conn != nil {
			_ = host.conn.Close()
		}
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

func (r *Runtime) WaitBootstrap(ctx context.Context, binding *runtimepb.Binding) (*emptypb.Empty, error) {
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	r.mu.Lock()
	valid := binding != nil && r.host != nil && r.host.connected && r.lastRunning != nil &&
		proto.Equal(binding, r.lastRunning) && binding.RuntimeBootId == r.bootID && binding.HostBootId == r.host.bootID
	r.mu.Unlock()
	if !valid {
		return nil, status.Error(codes.FailedPrecondition, "bootstrap wait is outside the admitted environment binding")
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
		return &emptypb.Empty{}, nil
	}
	if err := wait(ctx); err != nil {
		return nil, err
	}
	return &emptypb.Empty{}, nil
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
		if r.intent != nil && proto.Equal(r.intent.Binding, binding) && r.intent.ExecutionFenced {
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
		report, ready := r.currentReport(binding)
		if ready && report.Facts.GetQuiesceStage() == environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED {
			return nil
		}
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		if r.host == nil || !r.host.connected {
			r.mu.Unlock()
			return status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
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

func (r *Runtime) currentReport(binding *runtimepb.Binding) (*runtimepb.HostReport, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.host == nil || !r.host.connected || r.host.report == nil || !r.host.report.Ready || !proto.Equal(r.host.report.Binding, binding) {
		return nil, false
	}
	return proto.Clone(r.host.report).(*runtimepb.HostReport), true
}
