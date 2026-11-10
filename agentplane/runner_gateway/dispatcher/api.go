package dispatcher

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/apimachinery/pkg/util/sets"
)

// SandboxReader is the shared, UID-fenced live Sandbox view supplied by the
// unified process. A missing Sandbox is distinct from a suspended one.
type SandboxReader interface {
	GetSandbox(context.Context, string) (SandboxSnapshot, bool, error)
}

// ExecutionAccountReader resolves the current controller-owned ServiceAccount for a Sandbox, and the
// Identity its `spec.identity` names (nil, found when it names none). The UIDs are live
// delivery/write authority; retained reads use the UIDs recorded in session.
type ExecutionAccountReader interface {
	GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error)
	GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error)
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

// RunnerCommands delivers durable commands through the Gateway or central sampler.
type RunnerCommands interface {
	Deliver(context.Context, SandboxSnapshot, string, *commandpb.Command) (*eventlogpb.EventEntry, error)
}

type Service struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	Store *Store
	// DispatcherCallers retain the existing explicit Pod-bound backend path. An account-only
	// bearer for one of these names remains an ordinary execution-principal caller.
	DispatcherCallers sets.Set[auth.Account]
	Sandboxes         SandboxReader
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

func terminalVisibleTo(event TerminalEvent, caller Caller) bool {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		return c.Privileged || event.Owner != nil && c.ServiceAccountIdentity != nil && sameServiceAccountIdentity(event.Owner, c.ServiceAccountIdentity)
	case IdentityCaller:
		return event.OwnerIdentity != nil && sameIdentity(event.OwnerIdentity, c.Identity)
	default:
		return false
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

// Methods applies the caller's explicit authorization rule to every SessionDispatcher RPC. Handlers
// serve an IdentityAdmission as an IdentityCaller, so the rule may admit Identity callers.
func Methods(rule auth.Rule) auth.Methods {
	return auth.Methods{
		dispatcherpb.SessionDispatcher_SubmitInput_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_ChangeModel_FullMethodName:           rule,
		dispatcherpb.SessionDispatcher_ChangeReasoningEffort_FullMethodName: rule,
		dispatcherpb.SessionDispatcher_InterruptTurn_FullMethodName:         rule,
		dispatcherpb.SessionDispatcher_CreateSession_FullMethodName:         rule,
		dispatcherpb.SessionDispatcher_GetSession_FullMethodName:            rule,
		dispatcherpb.SessionDispatcher_ListSessions_FullMethodName:          rule,
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
	methods, err := Methods(auth.ReviewedServiceAccountOrIdentity(resolver)).WithDelegation(
		delegatedMethods, []auth.Delegator{delegator}, serviceAccounts,
	)
	if err != nil {
		return nil, fmt.Errorf("configure delegated Dispatcher methods: %w", err)
	}
	return methods, nil
}

func (s *Service) caller(ctx context.Context) (Caller, error) {
	admitted, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	if identity, ok := admitted.(auth.IdentityAdmission); ok {
		return identityCaller(identity.Caller)
	}
	return s.serviceAccountCaller(ctx)
}

func identityCaller(principal auth.IdentityPrincipal) (IdentityCaller, error) {
	if principal.Identity.Name == "" || principal.Identity.UID == "" || (principal.PodName == "") != (principal.PodUID == "") {
		return IdentityCaller{}, status.Error(codes.Unauthenticated, "Identity caller is incomplete")
	}
	caller := IdentityCaller{Identity: &identitypb.Identity{Name: principal.Identity.Name, Uid: principal.Identity.UID}}
	if principal.PodName != "" {
		caller.Pod = &PodIdentity{Name: principal.PodName, UID: principal.PodUID}
	}
	return caller, nil
}

func (s *Service) serviceAccountCaller(ctx context.Context) (ServiceAccountCaller, error) {
	effective, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return ServiceAccountCaller{}, err
	}
	if effective == nil || effective.GetAccount() == nil || effective.GetAccount().GetNamespace() == "" ||
		effective.GetAccount().GetName() == "" || effective.GetUid() == "" {
		return ServiceAccountCaller{}, status.Error(codes.Unauthenticated, "effective ServiceAccount identity is incomplete")
	}
	attribution := &AdmissionAttribution{Actor: effective, EffectiveCaller: effective}
	privileged := false
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return ServiceAccountCaller{}, err
	}
	switch call := admission.(type) {
	case auth.DirectAdmission:
		direct := call
		if direct.Pod != nil {
			attribution.ActorPod = &PodIdentity{Name: direct.Pod.PodName, UID: direct.Pod.PodUID}
			configured := s.DispatcherCallers.Has(auth.Account{Namespace: effective.GetAccount().GetNamespace(), Name: effective.GetAccount().GetName()})
			privileged = configured && direct.Pod.IsPodBound()
		}
	case auth.DelegatedAdmission:
		delegated := call
		attribution.Actor = &ServiceAccountIdentity{
			Account: &identitypb.ServiceAccount{Namespace: delegated.Actor.Account.Namespace, Name: delegated.Actor.Account.Name},
			Uid:     delegated.Actor.ServiceAccountUID,
		}
		attribution.EffectiveCaller = delegated.EffectiveCaller
		attribution.ActorPod = &PodIdentity{Name: delegated.Actor.PodName, UID: delegated.Actor.PodUID}
		attribution.Action = &ActionCorrelation{
			RequestID: delegated.Provenance.GetActionRequestId(), ExecutionID: delegated.Provenance.GetActionExecutionId(),
		}
	default:
		return ServiceAccountCaller{}, status.Error(codes.Unauthenticated, "call not admitted")
	}
	if err := validateAttribution(attribution, nil); err != nil {
		return ServiceAccountCaller{}, status.Error(codes.Unauthenticated, "call attribution is invalid")
	}
	return ServiceAccountCaller{
		ServiceAccountIdentity: effective,
		Privileged:             privileged, Attribution: attribution,
	}, nil
}

func (s *Service) executionAccount(ctx context.Context, sandbox SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	reader, ok := s.Sandboxes.(ExecutionAccountReader)
	if !ok {
		return nil, false, errors.New("Sandbox execution account reader unavailable")
	}
	return reader.GetExecutionAccount(ctx, sandbox)
}

func (s *Service) executionIdentity(ctx context.Context, sandbox SandboxSnapshot) (*identitypb.Identity, bool, error) {
	reader, ok := s.Sandboxes.(ExecutionAccountReader)
	if !ok {
		return nil, false, errors.New("Sandbox execution account reader unavailable")
	}
	return reader.GetExecutionIdentity(ctx, sandbox)
}

// requireLiveExecution checks the recorded target before applying scoped backend trust or
// proving that a normal caller still matches its current and retained execution account.
func (s *Service) requireLiveExecution(ctx context.Context, session *Session, sandbox SandboxSnapshot, caller Caller) error {
	if session == nil || session.SandboxName != sandbox.Name || session.SandboxUID != sandbox.UID {
		return status.Error(codes.PermissionDenied, "Session target does not match")
	}
	switch c := caller.(type) {
	case ServiceAccountCaller:
		return s.requireLiveAccount(ctx, session, sandbox, c)
	case IdentityCaller:
		return s.requireLiveIdentity(ctx, session, sandbox, c)
	default:
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
	}
}

// requireLiveIdentity proves an Identity caller owns the Session and its Sandbox still names that
// exact Identity incarnation.
func (s *Service) requireLiveIdentity(ctx context.Context, session *Session, sandbox SandboxSnapshot, caller IdentityCaller) error {
	if session.ExecutionIdentity == nil || !sameIdentity(session.ExecutionIdentity, caller.Identity) {
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
	}
	identity, found, err := s.executionIdentity(ctx, sandbox)
	if err != nil {
		return status.Error(codes.Unavailable, "sandbox execution identity unavailable")
	}
	if !found || !sameIdentity(identity, session.ExecutionIdentity) {
		return status.Error(codes.FailedPrecondition, "sandbox execution identity changed")
	}
	return nil
}

func (s *Service) requireLiveAccount(ctx context.Context, session *Session, sandbox SandboxSnapshot, caller ServiceAccountCaller) error {
	if caller.Privileged {
		return nil
	}
	if session.ExecutionPrincipal == nil || session.ExecutionPrincipal.GetUid() == "" {
		return status.Error(codes.PermissionDenied, "caller does not own the Session")
	}
	account, found, err := s.executionAccount(ctx, sandbox)
	if err != nil {
		return status.Error(codes.Unavailable, "sandbox execution identity unavailable")
	}
	if !found || account == nil || account.GetUid() != session.ExecutionPrincipal.GetUid() {
		return status.Error(codes.FailedPrecondition, "sandbox execution identity changed")
	}
	if caller.ServiceAccountIdentity == nil || !sameServiceAccount(account.GetAccount(), caller.ServiceAccountIdentity.GetAccount()) ||
		account.GetUid() != caller.ServiceAccountIdentity.GetUid() ||
		!sameServiceAccount(session.ExecutionPrincipal.GetAccount(), caller.ServiceAccountIdentity.GetAccount()) ||
		session.ExecutionPrincipal.GetUid() != caller.ServiceAccountIdentity.GetUid() {
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
	case errors.Is(err, ErrSessionConflict):
		return status.Error(codes.AlreadyExists, "session_id was opened with another request")
	case errors.Is(err, ErrSandboxDeleted):
		return status.Error(codes.FailedPrecondition, "sandbox was deleted")
	case errors.Is(err, ErrSessionUnconfirmed):
		return status.Error(codes.FailedPrecondition, "Session is not confirmed")
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

// commandTarget resolves the Session's placement and applies an optional incarnation-scoped
// authorization precondition before checking the live Sandbox and execution account.
func (s *Service) commandTarget(ctx context.Context, sessionID, expectedSandboxUID string, caller Caller) (*Session, SandboxSnapshot, error) {
	session, err := s.Store.GetSessionForCaller(ctx, sessionID, caller)
	if err != nil {
		return nil, SandboxSnapshot{}, apiError(err)
	}
	if expectedSandboxUID != "" && expectedSandboxUID != session.SandboxUID {
		return nil, SandboxSnapshot{}, status.Error(codes.PermissionDenied, "Session target does not match")
	}
	current, err := s.sandbox(ctx, session.SandboxName)
	if err != nil {
		return nil, SandboxSnapshot{}, err
	}
	if err := s.requireLiveExecution(ctx, session, current, caller); err != nil {
		return nil, SandboxSnapshot{}, err
	}
	return session, current, nil
}

func (s *Service) enqueue(ctx context.Context, commandID, sessionID, expectedSandboxUID string, command *commandpb.Command) (dispatch *dispatcherpb.Dispatch, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Dispatcher Enqueue",
		trace.WithAttributes(attribute.String("session.id", sessionID)),
	)
	defer func() { observability.End(span, resultErr) }()
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := required(commandID, sessionID); err != nil {
		return nil, err
	}
	session, _, err := s.commandTarget(ctx, sessionID, expectedSandboxUID, caller)
	if err != nil {
		return nil, err
	}
	row, err := s.Store.EnqueueForCaller(ctx, sessionID, session.SandboxName, session.SandboxUID, command, nil, caller)
	if err != nil {
		return nil, apiError(err)
	}
	return DescribeDispatch(row)
}

func (s *Service) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetExpectedSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_SubmitInput{
			SubmitInput: &commandpb.SubmitInput{Text: request.GetText()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.SubmitInputResponse{Dispatch: row}, nil
}

func (s *Service) ChangeModel(ctx context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetExpectedSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_ChangeModel{
			ChangeModel: &commandpb.ChangeModel{Model: request.GetModel()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ChangeModelResponse{Dispatch: row}, nil
}

func (s *Service) ChangeReasoningEffort(ctx context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetExpectedSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_ChangeReasoningEffort{
			ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: request.GetEffort()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.ChangeReasoningEffortResponse{Dispatch: row}, nil
}

func (s *Service) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (response *dispatcherpb.CreateSessionResponse, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Dispatcher CreateSession",
		trace.WithAttributes(
			attribute.String("session.id", request.GetSessionId()),
			attribute.String("sandbox.name", requestTarget(request).GetName()),
		),
	)
	defer func() { observability.End(span, resultErr) }()
	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	if err := canonicalSessionID(request.GetSessionId()); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	// An Identity creates no Session, not even on a Sandbox acting as it: acting as an Identity
	// confers no authority over the Sandbox. How a Session asks for a sub-Session is
	// SESSION_SUBAGENT_DELEGATION (agentplane/plans/task_dag.md).
	if _, ok := caller.(IdentityCaller); ok {
		return nil, status.Error(codes.PermissionDenied, "an Identity caller cannot create a Session")
	}
	// Recover an accepted request without consulting current owner availability. This
	// path only reads the receipt; it never queues, wakes, binds or materializes work.
	row, err := s.Store.GetSession(ctx, request.GetSessionId())
	if err == nil {
		if err := row.authorizeRetry(caller); err != nil {
			return nil, apiError(err)
		}
		placement, err := placementFromRequest(request)
		if err != nil {
			return nil, apiError(ErrSessionConflict)
		}
		target := requestTarget(request)
		if err := row.matchRequest(target.GetName(), target.GetUid(), placement, request.GetSpec(), request.SetupScript); err != nil {
			return nil, apiError(err)
		}
		session, err := DescribeSession(row)
		if err != nil {
			return nil, apiError(err)
		}
		return &dispatcherpb.CreateSessionResponse{Session: session}, nil
	}
	if !errors.Is(err, ErrNotFound) {
		return nil, apiError(err)
	}
	placement, err := placementFromRequest(request)
	if err != nil {
		return nil, err
	}
	target := requestTarget(request)
	current, err := s.sandbox(ctx, target.GetName())
	if err != nil {
		return nil, err
	}
	if current.UID != target.GetUid() {
		return nil, status.Error(codes.FailedPrecondition, "sandbox identity changed")
	}
	if err := ValidateRuntime(placement, request.SetupScript, s.CentralTarget); err != nil {
		return nil, err
	}
	// The Dispatcher is the authority for the Identity a Session executes as: the one its Sandbox
	// names now, read live with its UID, recorded once beside the ServiceAccount. A Sandbox naming an
	// Identity whose managed ServiceAccount is absent runs as that Identity alone: its Session
	// records no ServiceAccount.
	identity, identityFound, err := s.executionIdentity(ctx, current)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "sandbox execution identity unavailable")
	}
	account, accountFound, err := s.executionAccount(ctx, current)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "sandbox execution identity unavailable")
	}
	execution := SessionExecution{Identity: identity}
	if accountFound {
		execution.Principal = account
	} else if identity == nil {
		// Only a privileged open reaches the store without the account; delivery binds its UID.
		execution.Principal = &ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: current.Namespace, Name: current.Name}}
	}
	switch c := caller.(type) {
	case ServiceAccountCaller:
		if !c.Privileged && (!accountFound || account == nil || !sameServiceAccountIdentity(account, c.ServiceAccountIdentity)) {
			return nil, status.Error(codes.PermissionDenied, "caller does not own the Session")
		}
	}
	if !identityFound {
		return nil, status.Error(codes.FailedPrecondition, "sandbox execution identity unavailable")
	}
	row, err = s.Store.CreateSessionWithPlacementForCaller(ctx, request.GetSessionId(), target.GetName(), current.UID, placement,
		request.GetSpec(), request.SetupScript, nil, execution, caller)
	if err != nil {
		return nil, apiError(err)
	}
	if _, err := s.Store.GetSessionForCaller(ctx, row.SessionID, caller); err != nil {
		return nil, apiError(err)
	}
	session, err := DescribeSession(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.CreateSessionResponse{Session: session}, nil
}

func canonicalSessionID(value string) error {
	parsed, err := uuid.Parse(value)
	if err != nil || parsed.String() != value {
		return status.Error(codes.InvalidArgument, "session_id must be a canonical UUID")
	}
	return nil
}

func (s *Service) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	row, err := s.Store.GetSessionForCaller(ctx, request.GetSessionId(), caller)
	if err != nil {
		return nil, apiError(err)
	}
	session, err := DescribeSession(row)
	if err != nil {
		return nil, apiError(err)
	}
	return &dispatcherpb.GetSessionResponse{Session: session}, nil
}

func (s *Service) ListSessions(ctx context.Context, request *dispatcherpb.ListSessionsRequest) (*dispatcherpb.ListSessionsResponse, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	limit := request.GetLimit()
	if limit == 0 {
		limit = 50
	}
	if limit > 100 {
		return nil, status.Error(codes.InvalidArgument, "limit must be between 1 and 100")
	}
	var after string
	if token := request.GetPageToken(); token != "" {
		if len(token) > 128 {
			return nil, status.Error(codes.InvalidArgument, "invalid session page token")
		}
		decoded, err := base64.RawURLEncoding.DecodeString(token)
		if err != nil || canonicalSessionID(string(decoded)) != nil {
			return nil, status.Error(codes.InvalidArgument, "invalid session page token")
		}
		after = string(decoded)
	}
	rows, err := s.Store.ListSessionsForCaller(ctx, after, int(limit)+1, caller)
	if err != nil {
		return nil, apiError(err)
	}
	response := &dispatcherpb.ListSessionsResponse{}
	if len(rows) > int(limit) {
		rows = rows[:limit]
		response.NextPageToken = base64.RawURLEncoding.EncodeToString([]byte(rows[len(rows)-1].SessionID))
	}
	for _, row := range rows {
		session, err := DescribeSession(row)
		if err != nil {
			return nil, apiError(err)
		}
		response.Sessions = append(response.Sessions, session)
	}
	return response, nil
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
	if !privileged(caller) {
		session, openErr := s.Store.GetSession(ctx, queued.SessionID)
		if openErr != nil {
			return nil, apiError(openErr)
		}
		current, err := s.sandbox(ctx, session.SandboxName)
		if err != nil {
			return nil, err
		}
		if err := s.requireLiveExecution(ctx, session, current, caller); err != nil {
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
	if err := required(request.GetTurnId()); err != nil {
		return nil, err
	}
	row, err := s.enqueue(ctx, request.GetCommandId(), request.GetSessionId(), request.GetExpectedSandboxUid(),
		&commandpb.Command{CommandId: request.GetCommandId(), Operation: &commandpb.Command_InterruptTurn{
			InterruptTurn: &commandpb.InterruptTurn{TurnId: request.GetTurnId()},
		}})
	if err != nil {
		return nil, err
	}
	return &dispatcherpb.InterruptTurnResponse{Dispatch: row}, nil
}
