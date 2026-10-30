// Package execution translates typed Runner RPCs to the local Codex exec-server protocol.
package execution

import (
	"context"
	"errors"
	"sync"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const maxNativeBytes = 1 << 20

// maxOpenConnections is an inherited Runner resource guard, not a Codex protocol limit.
const maxOpenConnections = 8

type Config struct {
	Context context.Context
	// Admit authorizes the scope and fences lifecycle changes while accepting a call.
	// opening distinguishes a new local executor session from work on an open session.
	Admit func(ctx context.Context, scope *executorpb.ExecutionScope, opening bool, accept func(context.Context) error) error
	// Acquire requests the controller-owned executor and waits for its ready local endpoint.
	Acquire  func(context.Context) (string, error)
	OnChange func()
}
type Adapter struct {
	executorpb.UnimplementedExecutorServer
	config   Config
	ctx      context.Context
	cancel   context.CancelFunc
	mu       sync.Mutex
	drainMu  sync.Mutex
	sessions map[string]*connection
	slots    map[uint64]context.CancelFunc
	slotDone map[uint64]chan struct{}
	nextSlot uint64
	opening  int
	workers  sync.WaitGroup
	draining bool
	closed   bool
}

func New(config Config) (*Adapter, error) {
	if config.Context == nil || config.Admit == nil || config.Acquire == nil {
		return nil, errors.New("execution context, admission and acquisition are required")
	}
	ctx, cancel := context.WithCancel(config.Context)
	return &Adapter{config: config, ctx: ctx, cancel: cancel, sessions: map[string]*connection{}, slots: map[uint64]context.CancelFunc{}, slotDone: map[uint64]chan struct{}{}}, nil
}

func validateScope(scope *executorpb.ExecutionScope) error {
	if scope == nil || scope.Destination == nil || scope.ExpectedIncarnationUid == "" || scope.SessionId == "" {
		return status.Error(codes.InvalidArgument, "execution destination, incarnation and Session are required")
	}
	return nil
}

func (a *Adapter) changed() {
	if a.config.OnChange != nil {
		a.config.OnChange()
	}
}

func (a *Adapter) Active() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.opening > 0 || len(a.sessions) > 0
}

func (a *Adapter) Drain(ctx context.Context) error {
	a.drainMu.Lock()
	defer a.drainMu.Unlock()
	a.mu.Lock()
	if a.closed || a.ctx.Err() != nil {
		a.mu.Unlock()
		return a.wait(ctx)
	}
	a.draining = true
	cancels := make([]context.CancelFunc, 0, len(a.slots))
	for _, cancel := range a.slots {
		cancels = append(cancels, cancel)
	}
	a.mu.Unlock()
	a.changed()
	for _, cancel := range cancels {
		cancel()
	}
	err := a.wait(ctx)
	a.mu.Lock()
	if !a.closed && a.ctx.Err() == nil {
		a.draining = false
	}
	a.mu.Unlock()
	a.changed()
	return err
}

func (a *Adapter) wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { a.workers.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (a *Adapter) Close() error {
	a.mu.Lock()
	if !a.closed {
		a.closed = true
		a.cancel()
	}
	a.mu.Unlock()
	a.changed()
	return a.wait(context.Background())
}

func (a *Adapter) authorize(ctx context.Context, scope *executorpb.ExecutionScope, opening bool, f func(context.Context) error) error {
	if err := validateScope(scope); err != nil {
		return err
	}
	return a.config.Admit(ctx, scope, opening, f)
}

// Once accepted, the native call belongs to the open session. A canceled unary RPC
// stops waiting without replaying the call or claiming that its process was stopped.
func (a *Adapter) execute(ctx context.Context, scope *executorpb.ExecutionScope, requestID, sid string, work func(*connection, *[]*executorpb.ExecutionNativeFrame) error) (*executorpb.ExecutionStatus, []*executorpb.ExecutionNativeFrame, error) {
	if requestID == "" || len(requestID) > 256 {
		return nil, nil, invalid("request ID is required and limited to 256 bytes")
	}
	type result struct {
		status *executorpb.ExecutionStatus
		frames []*executorpb.ExecutionNativeFrame
	}
	done := make(chan result, 1)
	err := a.authorize(ctx, scope, false, func(context.Context) error {
		a.mu.Lock()
		defer a.mu.Unlock()
		if a.closed || a.draining || a.ctx.Err() != nil {
			return status.Error(codes.Unavailable, "execution adapter is stopping")
		}
		c := a.sessions[sid]
		if c == nil {
			return status.Error(codes.FailedPrecondition, "execution session connection is closed")
		}
		if err := c.ctx.Err(); err != nil {
			return status.Error(codes.FailedPrecondition, "execution session connection is closing")
		}
		if !proto.Equal(c.scope, scope) {
			return status.Error(codes.PermissionDenied, "execution session belongs to a different scope")
		}
		select {
		case c.calls <- struct{}{}:
		default:
			return status.Error(codes.ResourceExhausted, "execution session has too many outstanding calls")
		}
		a.workers.Add(1)
		go func() {
			defer a.workers.Done()
			defer func() { <-c.calls }()
			frames := []*executorpb.ExecutionNativeFrame{}
			outcome := executionOutcome(work(c, &frames))
			done <- result{status: outcome, frames: frames}
		}()
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	select {
	case result := <-done:
		return result.status, result.frames, nil
	case <-ctx.Done():
		return nil, nil, status.FromContextError(ctx.Err()).Err()
	}
}

func executionOutcome(err error) *executorpb.ExecutionStatus {
	if err == nil {
		return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED}
	}
	var rejected *executorRejectedError
	if errors.As(err, &rejected) {
		code := int32(rejected.code)
		return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: rejected.message, UpstreamCode: &code}
	}
	var limited *nativeRequestLimitError
	if errors.As(err, &limited) {
		return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: limited.Error()}
	}
	var admissionRejected *executionAdmissionRejectedError
	if errors.As(err, &admissionRejected) {
		return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: admissionRejected.message}
	}
	return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "The native call outcome is unknown; no automatic retry was attempted: " + err.Error()}
}
func invalid(message string) error { return status.Error(codes.InvalidArgument, message) }
