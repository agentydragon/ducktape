package dispatcher

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// SandboxReader is the shared, UID-fenced live Sandbox view supplied by the
// unified process. A missing Sandbox is distinct from a suspended one.
type SandboxReader interface {
	GetSandbox(context.Context, string) (SandboxSnapshot, bool, error)
}

type SandboxSnapshot struct {
	Name, Namespace, UID string
	// Blocker is the current dispatch reason, empty only when the runner is Ready.
	Blocker     string
	WakeOnSend  bool
	Incarnation string
	Deleting    bool
	ReadyDetail string
}

// RunnerCommands uses the Gateway's generated Runner proxy for direct
// InterruptTurn. Durable commands are delivered by the worker, not this RPC.
type RunnerCommands interface {
	Deliver(context.Context, SandboxSnapshot, string, *commandpb.Command) (*eventlogpb.EventEntry, error)
}

type Service struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	Store       *Store
	Auth        *auth.Resolver
	Callers     map[auth.Account]struct{}
	WakeCallers map[auth.Account]struct{}
	Sandboxes   SandboxReader
	Runner      RunnerCommands
	Hub         *WatchHub
	WatchLease  time.Duration
}

// maxFilterCommands bounds DispatchFilter.command_ids, the size of a Session's queue.
const maxFilterCommands = 1000

// watchWriteTimeout bounds how long a slow reader can hold a Dispatcher watch.
const watchWriteTimeout = 5 * time.Second

func inScope(filter *dispatcherpb.DispatchFilter, row *dispatcherpb.Dispatch) bool {
	if filter == nil {
		return false
	}
	if ids := filter.GetCommandIds(); len(ids) > 0 && !slices.Contains(ids, row.GetCommandId()) {
		return false
	}
	switch scope := filter.GetScope().(type) {
	case *dispatcherpb.DispatchFilter_SessionId:
		return row.GetSessionId() == scope.SessionId
	case *dispatcherpb.DispatchFilter_Sandbox:
		return row.GetSandbox() == scope.Sandbox
	default:
		return false
	}
}

func (s *Service) WatchDispatches(request *dispatcherpb.WatchDispatchesRequest, stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse]) error {
	ctx := stream.Context()
	if _, err := s.caller(ctx); err != nil {
		return err
	}
	if s.Hub == nil {
		return status.Error(codes.Unavailable, "dispatcher watch listener unavailable")
	}
	sub, unsubscribe, err := s.Hub.subscribe()
	if err != nil {
		return status.Error(codes.Unavailable, "dispatcher watch listener unavailable")
	}
	defer unsubscribe()
	lease := s.WatchLease
	if lease <= 0 {
		return status.Error(codes.Internal, "watch lease must be positive")
	}
	leaseCtx, endLease := context.WithTimeout(ctx, lease)
	defer endLease()
	written := map[string]struct {
		State  dispatcherpb.DeliveryState
		Reason string
	}{}
	var terminal []TerminalEvent
	changed, caughtUp := true, false
	for {
		if changed {
			changed = false
			rows, err := s.list(ctx, request.GetFilter())
			if err != nil {
				return err
			}
			for _, event := range terminal {
				if inScope(request.GetFilter(), event.Dispatch) {
					rows = append(rows, event.Dispatch)
				}
			}
			terminal = nil
			for _, row := range rows {
				key := struct {
					State  dispatcherpb.DeliveryState
					Reason string
				}{row.GetState(), row.GetReason()}
				if written[row.GetCommandId()] == key {
					continue
				}
				ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
					Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: row},
				})
				if err != nil {
					return err
				}
				if ended {
					return nil
				}
				written[row.GetCommandId()] = key
			}
			if !caughtUp {
				caughtUp = true
				ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
					Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
				})
				if err != nil {
					return err
				}
				if ended {
					return nil
				}
			}
		}
		select {
		case <-leaseCtx.Done():
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return nil
		case message := <-sub.failure:
			return status.Error(codes.Unavailable, message)
		case event := <-sub.terminal:
			terminal = append(terminal, event)
			changed = true
		case <-sub.changed:
			changed = true
		}
	}
}

func sendWatchDispatch(
	leaseCtx, ctx context.Context,
	stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse],
	frame *dispatcherpb.WatchDispatchesResponse,
) (bool, error) {
	err := streaming.SendWithin(leaseCtx, watchWriteTimeout, stream.Send, frame)
	switch {
	case err == nil:
		return false, nil
	case ctx.Err() != nil:
		return false, ctx.Err()
	case leaseCtx.Err() != nil:
		// The lease ended; the caller renews with a new authorized watch.
		return true, nil
	case errors.Is(err, streaming.ErrSendTimeout):
		return false, status.Error(codes.DeadlineExceeded, "dispatcher watch write deadline expired")
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return false, status.FromContextError(err).Err()
	default:
		return false, err
	}
}

func (s *Service) caller(ctx context.Context) (*Account, error) {
	principal, err := s.Auth.RequireAccount(ctx, s.Callers)
	if err != nil {
		return nil, err
	}
	if _, ok := s.WakeCallers[principal.Account]; ok {
		return &Account{Namespace: principal.Account.Namespace, Name: principal.Account.Name}, nil
	}
	return nil, nil
}

func apiError(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, ErrCommandConflict):
		return status.Error(codes.AlreadyExists, "command_id was used for other work")
	case errors.Is(err, ErrOpeningConflict):
		return status.Error(codes.AlreadyExists, "session_id was opened with another request")
	case errors.Is(err, ErrQueueFull):
		return status.Error(codes.ResourceExhausted, err.Error())
	case errors.Is(err, ErrNotFound):
		return status.Error(codes.NotFound, "dispatch not found")
	case errors.Is(err, ErrNotCancellable):
		return status.Error(codes.FailedPrecondition, "delivery already began")
	default:
		return status.Error(codes.Unavailable, "dispatcher storage unavailable")
	}
}

func required(values ...string) error {
	for _, value := range values {
		if value == "" {
			return status.Error(codes.InvalidArgument, "required dispatcher field is empty")
		}
	}
	return nil
}

func (s *Service) sandbox(ctx context.Context, name string) (SandboxSnapshot, error) {
	current, found, err := s.Sandboxes.GetSandbox(ctx, name)
	if err != nil {
		return SandboxSnapshot{}, status.Error(codes.Unavailable, "sandbox inventory unavailable")
	}
	if !found {
		return SandboxSnapshot{}, status.Error(codes.NotFound, "sandbox not found")
	}
	return current, nil
}

func (s *Service) enqueue(ctx context.Context, commandID, sessionID, sandbox, sandboxUID string, command *commandpb.Command) (dispatch *dispatcherpb.Dispatch, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Dispatcher Enqueue",
		trace.WithAttributes(attribute.String("session.id", sessionID), attribute.String("sandbox.name", sandbox)),
	)
	defer func() { observability.End(span, resultErr) }()
	wake, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := required(commandID, sessionID, sandbox, sandboxUID); err != nil {
		return nil, err
	}
	current, err := s.sandbox(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	if current.UID != sandboxUID {
		return nil, status.Error(codes.FailedPrecondition, "sandbox identity changed")
	}
	row, err := s.Store.Enqueue(ctx, sessionID, sandbox, current.UID, command, wake)
	if err != nil {
		return nil, apiError(err)
	}
	return DescribeDispatch(row)
}

func (s *Service) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetSandbox(), request.GetSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_SubmitInput{
			SubmitInput: &commandpb.SubmitInput{Text: request.GetText()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: row}, nil
}

func (s *Service) ChangeModel(ctx context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetSandbox(), request.GetSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_ChangeModel{
			ChangeModel: &commandpb.ChangeModel{Model: request.GetModel()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ChangeModelResponse{Dispatch: row}, nil
}

func (s *Service) ChangeReasoningEffort(ctx context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetSandbox(), request.GetSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_ChangeReasoningEffort{
			ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: request.GetEffort()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ChangeReasoningEffortResponse{Dispatch: row}, nil
}

func (s *Service) OpenSession(ctx context.Context, request *dispatcherpb.OpenSessionRequest) (response *dispatcherpb.OpenSessionResponse, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Dispatcher OpenSession",
		trace.WithAttributes(
			attribute.String("session.id", request.GetSessionId()),
			attribute.String("sandbox.name", request.GetSandbox()),
		),
	)
	defer func() { observability.End(span, resultErr) }()
	wake, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := required(request.GetSessionId(), request.GetSandbox()); err != nil {
		return nil, err
	}
	current, err := s.sandbox(ctx, request.GetSandbox())
	if err != nil {
		return nil, err
	}
	row, err := s.Store.OpenSession(ctx, request.GetSessionId(), request.GetSandbox(), current.UID,
		request.GetSpec(), request.SetupScript, wake)
	if err != nil {
		return nil, apiError(err)
	}
	opening, err := DescribeOpening(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.OpenSessionResponse{Opening: opening}, nil
}

func (s *Service) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	if _, err := s.caller(ctx); err != nil {
		return nil, err
	}
	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	row, err := s.Store.GetOpening(ctx, request.GetSessionId())
	if err != nil {
		return nil, apiError(err)
	}
	opening, err := DescribeOpening(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.GetSessionOpeningResponse{Opening: opening}, nil
}

func (s *Service) GetDispatch(ctx context.Context, request *dispatcherpb.GetDispatchRequest) (*dispatcherpb.GetDispatchResponse, error) {
	if _, err := s.caller(ctx); err != nil {
		return nil, err
	}
	if err := required(request.GetCommandId()); err != nil {
		return nil, err
	}
	row, err := s.Store.GetDispatch(ctx, request.GetCommandId())
	if err != nil {
		return nil, apiError(err)
	}
	dispatch, err := DescribeDispatch(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.GetDispatchResponse{Dispatch: dispatch}, nil
}

func (s *Service) CancelDispatch(ctx context.Context, request *dispatcherpb.CancelDispatchRequest) (*dispatcherpb.CancelDispatchResponse, error) {
	if _, err := s.caller(ctx); err != nil {
		return nil, err
	}
	if err := required(request.GetCommandId()); err != nil {
		return nil, err
	}
	row, err := s.Store.Cancel(ctx, request.GetCommandId())
	if err != nil {
		return nil, apiError(err)
	}
	dispatch, err := DescribeDispatch(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.CancelDispatchResponse{Dispatch: dispatch}, nil
}

func (s *Service) list(ctx context.Context, filter *dispatcherpb.DispatchFilter) ([]*dispatcherpb.Dispatch, error) {
	if filter == nil {
		return nil, status.Error(codes.InvalidArgument, "dispatch filter is required")
	}
	var session, sandbox string
	switch scope := filter.GetScope().(type) {
	case *dispatcherpb.DispatchFilter_SessionId:
		session = scope.SessionId
	case *dispatcherpb.DispatchFilter_Sandbox:
		sandbox = scope.Sandbox
	default:
		return nil, status.Error(codes.InvalidArgument, "dispatch filter is required")
	}
	if len(filter.GetCommandIds()) > maxFilterCommands {
		return nil, status.Errorf(codes.InvalidArgument, "dispatch filter names more than %d commands", maxFilterCommands)
	}
	rows, err := s.Store.ListDispatches(ctx, session, sandbox, filter.GetCommandIds())
	if err != nil {
		return nil, apiError(err)
	}
	result := make([]*dispatcherpb.Dispatch, 0, len(rows))
	for _, row := range rows {
		dispatch, err := DescribeDispatch(row)
		if err != nil {
			return nil, apiError(err)
		}
		result = append(result, dispatch)
	}
	return result, nil
}

func (s *Service) ListDispatches(ctx context.Context, request *dispatcherpb.ListDispatchesRequest) (*dispatcherpb.ListDispatchesResponse, error) {
	if _, err := s.caller(ctx); err != nil {
		return nil, err
	}
	rows, err := s.list(ctx, request.GetFilter())
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ListDispatchesResponse{Dispatches: rows}, nil
}

func (s *Service) InterruptTurn(ctx context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	if _, err := s.caller(ctx); err != nil {
		return nil, err
	}
	if err := required(request.GetCommandId(), request.GetSessionId(), request.GetSandbox(), request.GetSandboxUid(), request.GetTurnId()); err != nil {
		return nil, err
	}
	current, err := s.sandbox(ctx, request.GetSandbox())
	if err != nil {
		return nil, err
	}
	if current.UID != request.GetSandboxUid() {
		return nil, status.Error(codes.FailedPrecondition, "sandbox identity changed")
	}
	if current.Blocker != "" {
		return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP, Reason: &current.Blocker}, nil
	}
	if s.Runner == nil {
		return nil, status.Error(codes.Unavailable, "runner proxy unavailable")
	}
	admission, err := s.Runner.Deliver(ctx, current, request.GetSessionId(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_InterruptTurn{
			InterruptTurn: &commandpb.InterruptTurn{TurnId: request.GetTurnId()},
		}})
	if err != nil {
		return nil, status.Error(status.Code(err), fmt.Sprintf("runner: %s", status.Convert(err).Message()))
	}
	return &dispatcherpb.InterruptTurnResponse{State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED, Admission: admission}, nil
}
