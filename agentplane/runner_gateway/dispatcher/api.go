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
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// SandboxReader is the shared, UID-fenced live Sandbox view supplied by the
// unified process. A missing Sandbox is distinct from a suspended one.
type SandboxReader interface {
	GetSandbox(context.Context, string) (SandboxSnapshot, bool, error)
}

// ExecutionAccountReader resolves the current controller-owned ServiceAccount for a Sandbox. The
// UID is live delivery/write authority; retained reads use the UID recorded in session_opening.
type ExecutionAccountReader interface {
	GetExecutionAccount(context.Context, SandboxSnapshot) (ServiceAccountIdentity, bool, error)
}

type SandboxSnapshot struct {
	Name, Namespace, UID string
	// Blocker is the current dispatch reason, empty only when the runner is Ready.
	Blocker        string
	WakeOnSend     bool
	WakeGeneration int64
	ManualStop     bool
	Incarnation    string
	Deleting       bool
	ReadyDetail    string
}

// RunnerCommands uses the Gateway's generated Runner proxy for direct
// InterruptTurn. Durable commands are delivered by the worker, not this RPC.
type RunnerCommands interface {
	Deliver(context.Context, SandboxSnapshot, string, *commandpb.Command) (*eventlogpb.EventEntry, error)
}

type Service struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	Store *Store
	// DispatcherCallers retain the existing explicit Pod-bound backend path. An account-only
	// bearer for one of these names remains an ordinary execution-principal caller.
	DispatcherCallers map[auth.Account]struct{}
	Sandboxes         SandboxReader
	Runner            RunnerCommands
	Hub               *WatchHub
	WatchLease        time.Duration
	CentralTarget     *CentralTarget
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
	caller, err := s.caller(ctx)
	if err != nil {
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
	written := map[string]*dispatcherpb.Dispatch{}
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
				if terminalVisibleTo(event, caller) && inScope(request.GetFilter(), event.Dispatch) {
					rows = append(rows, event.Dispatch)
				}
			}
			terminal = nil
			for _, row := range rows {
				if previous, ok := written[row.GetCommandId()]; ok && proto.Equal(previous, row) {
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
				written[row.GetCommandId()] = proto.Clone(row).(*dispatcherpb.Dispatch)
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

func terminalVisibleTo(event TerminalEvent, caller CallerIdentity) bool {
	if caller.Privileged {
		return true
	}
	return event.Owner != nil && event.Owner.Account == caller.Account && event.Owner.UID == caller.UID
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

// Methods admits callers to every SessionDispatcher RPC.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		dispatcherpb.SessionDispatcher_SubmitInput_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_ChangeModel_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_ChangeReasoningEffort_FullMethodName: rule,
		dispatcherpb.SessionDispatcher_InterruptTurn_FullMethodName:         rule,
		dispatcherpb.SessionDispatcher_OpenSession_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_GetSessionOpening_FullMethodName:     rule,
		dispatcherpb.SessionDispatcher_GetDispatch_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_WatchDispatches_FullMethodName:       rule,
		dispatcherpb.SessionDispatcher_ListDispatches_FullMethodName:        rule,
		dispatcherpb.SessionDispatcher_CancelDispatch_FullMethodName:        rule,
	}
}

// MethodsWithDelegation adds Actions trust to only the configured full RPC method names.
func MethodsWithDelegation(
	resolver *auth.Resolver,
	delegator auth.Delegator,
	serviceAccounts auth.ServiceAccountUIDReader,
	delegatedMethods []string,
) (auth.Methods, error) {
	methods, err := Methods(resolver).WithDelegation(
		delegatedMethods, []auth.Delegator{delegator}, serviceAccounts,
	)
	if err != nil {
		return nil, fmt.Errorf("configure delegated Dispatcher methods: %w", err)
	}
	return methods, nil
}

func (s *Service) caller(ctx context.Context) (CallerIdentity, error) {
	effective, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return CallerIdentity{}, err
	}
	attribution := &AdmissionAttribution{
		Actor: ServiceAccountIdentity{
			Account: Account{Namespace: effective.Account.Namespace, Name: effective.Account.Name},
			UID:     effective.ServiceAccountUID,
		},
		EffectiveCaller: ServiceAccountIdentity{
			Account: Account{Namespace: effective.Account.Namespace, Name: effective.Account.Name},
			UID:     effective.ServiceAccountUID,
		},
	}
	privileged := false
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return CallerIdentity{}, err
	}
	switch call := admission.(type) {
	case auth.DirectAdmission:
		direct := call
		if direct.Pod != nil {
			attribution.ActorPod = &PodIdentity{Name: direct.Pod.PodName, UID: direct.Pod.PodUID}
			_, configured := s.DispatcherCallers[effective.Account]
			privileged = configured && direct.Pod.IsPodBound()
		}
	case auth.DelegatedAdmission:
		delegated := call
		attribution.Actor = ServiceAccountIdentity{
			Account: Account{Namespace: delegated.Actor.Account.Namespace, Name: delegated.Actor.Account.Name},
			UID:     delegated.Actor.ServiceAccountUID,
		}
		attribution.EffectiveCaller = ServiceAccountIdentity{
			Account: Account{Namespace: delegated.EffectiveCaller.Account.Namespace, Name: delegated.EffectiveCaller.Account.Name},
			UID:     delegated.EffectiveCaller.ServiceAccountUID,
		}
		attribution.ActorPod = &PodIdentity{Name: delegated.Actor.PodName, UID: delegated.Actor.PodUID}
		attribution.Action = &ActionCorrelation{
			RequestID: delegated.Provenance.GetActionRequestId(), ExecutionID: delegated.Provenance.GetActionExecutionId(),
		}
	default:
		return CallerIdentity{}, status.Error(codes.Unauthenticated, "call not admitted")
	}
	if err := validateAttribution(attribution); err != nil {
		return CallerIdentity{}, status.Error(codes.Unauthenticated, "call attribution is invalid")
	}
	return CallerIdentity{
		ServiceAccountIdentity: ServiceAccountIdentity{
			Account: Account{Namespace: effective.Account.Namespace, Name: effective.Account.Name},
			UID:     effective.ServiceAccountUID,
		},
		Privileged: privileged, Attribution: attribution,
	}, nil
}

func (s *Service) executionAccount(ctx context.Context, sandbox SandboxSnapshot) (ServiceAccountIdentity, bool, error) {
	reader, ok := s.Sandboxes.(ExecutionAccountReader)
	if !ok {
		return ServiceAccountIdentity{}, false, errors.New("Sandbox execution account reader unavailable")
	}
	return reader.GetExecutionAccount(ctx, sandbox)
}

// requireLiveExecution proves that a normal caller is still the exact ServiceAccount recorded for
// the target Sandbox. Privileged backend callers retain their explicit compatibility path.
func (s *Service) requireLiveExecution(ctx context.Context, opening *Opening, sandbox SandboxSnapshot, caller CallerIdentity) error {
	if caller.Privileged {
		return nil
	}
	if opening == nil || opening.ExecutionPrincipal == nil || opening.ExecutionPrincipal.UID == "" ||
		opening.SandboxName != sandbox.Name || opening.SandboxUID != sandbox.UID {
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
	}
	account, found, err := s.executionAccount(ctx, sandbox)
	if err != nil {
		return status.Error(codes.Unavailable, "sandbox execution identity unavailable")
	}
	if !found || account.UID != opening.ExecutionPrincipal.UID {
		return status.Error(codes.FailedPrecondition, "sandbox execution identity changed")
	}
	if account.Account != caller.Account || account.UID != caller.UID ||
		opening.ExecutionPrincipal.Account != caller.Account || opening.ExecutionPrincipal.UID != caller.UID {
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
	}
	return nil
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
	case errors.Is(err, ErrNotAuthorized):
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
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
	caller, err := s.caller(ctx)
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
	opening, openingErr := s.Store.GetOpening(ctx, sessionID)
	if openingErr != nil && !errors.Is(openingErr, ErrNotFound) {
		return nil, apiError(openingErr)
	}
	if openingErr == nil {
		if err := s.requireLiveExecution(ctx, opening, current, caller); err != nil {
			return nil, err
		}
	} else if !caller.Privileged {
		return nil, status.Error(codes.NotFound, "dispatch not found")
	}
	row, err := s.Store.EnqueueForCaller(ctx, sessionID, sandbox, current.UID, command, nil, caller)
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
	if err := required(request.GetSessionId(), request.GetSandbox(), request.GetSandboxUid()); err != nil {
		return nil, err
	}
	current, err := s.sandbox(ctx, request.GetSandbox())
	if err != nil {
		return nil, err
	}
	if current.UID != request.GetSandboxUid() {
		return nil, status.Error(codes.FailedPrecondition, "sandbox identity changed")
	}
	if err := ValidateRuntime(request.GetSpec(), request.SetupScript, s.CentralTarget); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	execution := ServiceAccountIdentity{Account: Account{Namespace: current.Namespace, Name: current.Name}}
	if caller.Privileged {
		account, found, err := s.executionAccount(ctx, current)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "sandbox execution identity unavailable")
		}
		if found {
			execution = account
		}
	} else {
		account, found, err := s.executionAccount(ctx, current)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "sandbox execution identity unavailable")
		}
		if !found || account != caller.ServiceAccountIdentity {
			return nil, status.Error(codes.PermissionDenied, "caller does not own the Session")
		}
		execution = account
	}
	row, err := s.Store.OpenSessionWithIdentity(ctx, request.GetSessionId(), request.GetSandbox(), current.UID,
		request.GetSpec(), request.SetupScript, nil, &execution, &caller.Attribution.Actor, caller.Attribution)
	if err != nil {
		return nil, apiError(err)
	}
	if _, err := s.Store.GetOpeningForCaller(ctx, row.SessionID, caller); err != nil {
		return nil, apiError(err)
	}
	opening, err := DescribeOpening(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.OpenSessionResponse{Opening: opening}, nil
}

func (s *Service) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	row, err := s.Store.GetOpeningForCaller(ctx, request.GetSessionId(), caller)
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
	if err := required(request.GetCommandId()); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	row, err := s.Store.GetDispatchForCaller(ctx, request.GetCommandId(), caller)
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
	if err := required(request.GetCommandId()); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	queued, err := s.Store.GetDispatchForCaller(ctx, request.GetCommandId(), caller)
	if err != nil {
		return nil, apiError(err)
	}
	if !caller.Privileged {
		opening, openErr := s.Store.GetOpening(ctx, queued.SessionID)
		if openErr != nil {
			return nil, apiError(openErr)
		}
		current, err := s.sandbox(ctx, opening.SandboxName)
		if err != nil {
			return nil, err
		}
		if err := s.requireLiveExecution(ctx, opening, current, caller); err != nil {
			return nil, err
		}
	}
	row, err := s.Store.CancelForCaller(ctx, request.GetCommandId(), caller)
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
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
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
	rows, err := s.Store.ListDispatchesForCaller(ctx, session, sandbox, filter.GetCommandIds(), caller)
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
	rows, err := s.list(ctx, request.GetFilter())
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ListDispatchesResponse{Dispatches: rows}, nil
}

func (s *Service) InterruptTurn(ctx context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
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
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.Store.AuthorizeTarget(ctx, request.GetSessionId(), current.Name, current.UID, caller); err != nil {
		return nil, apiError(err)
	}
	opening, openingErr := s.Store.GetOpening(ctx, request.GetSessionId())
	if openingErr != nil && !errors.Is(openingErr, ErrNotFound) {
		return nil, apiError(openingErr)
	}
	if openingErr == nil {
		if err := s.requireLiveExecution(ctx, opening, current, caller); err != nil {
			return nil, err
		}
	}
	requiresReady, err := s.requiresReadyExecutor(ctx, request.GetSessionId(), current)
	if err != nil {
		return nil, err
	}
	if current.Blocker != "" && requiresReady {
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

// Delivery behavior comes from admitted Session configuration, never a command claim.
func (s *Service) requiresReadyExecutor(ctx context.Context, id string, sandbox SandboxSnapshot) (bool, error) {
	opening, err := s.Store.GetOpening(ctx, id)
	if errors.Is(err, ErrNotFound) {
		return true, nil
	}
	if err != nil {
		return false, apiError(err)
	}
	if sessionRequiresReadyExecutor(opening.Spec) {
		return true, nil
	}
	if err := validateOpeningExecutor(opening, sandbox); err != nil {
		return false, err
	}
	return false, nil
}
