// Package sessiondelegation runs reviewed Actions at the authority of their authenticated caller.
package sessiondelegation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/session_delegation/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	sessionclient "github.com/agentydragon/ducktape/agentplane/session_client"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/workload_auth/grpc"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// Executor forwards the identity recorded when an Action was admitted. Its destination clients use only
// Actions' projected audience credentials plus that explicit, per-call delegation envelope.
type Executor struct {
	group      string
	binding    catalog.SessionDelegationBinding
	discovery  discoverypb.EnvironmentDiscoveryClient
	dispatcher dispatcherpb.SessionDispatcherClient
	history    historypb.HistoryServiceClient
	inputs     contract.Inputs
}

func NewExecutor(
	group string,
	binding catalog.SessionDelegationBinding,
	discovery discoverypb.EnvironmentDiscoveryClient,
	dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient,
) (*Executor, error) {
	inputs, err := contract.NewInputs()
	if err != nil {
		return nil, err
	}
	return &Executor{group: group, binding: binding, discovery: discovery, dispatcher: dispatcher, history: history, inputs: inputs}, nil
}

// Actions are only the implemented, reviewed operations belonging to this ActionGroup.
func (e *Executor) Actions() map[string]catalog.ActionDefinition {
	switch e.group {
	case contract.GroupEnvironments:
		return e.inputs.EnvironmentActions()
	case contract.GroupSessions:
		return e.inputs.SessionActions()
	default:
		return map[string]catalog.ActionDefinition{}
	}
}

func (e *Executor) Execute(ctx context.Context, request store.ExecutionRequest, lease *worker.Lease) (store.ExecutionOutcome, error) {
	return worker.Hold(ctx, lease, func(ctx context.Context) (store.ExecutionOutcome, error) {
		return e.attempt(ctx, request)
	})
}

func (e *Executor) attempt(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	delegated, err := e.delegatedContext(ctx, request)
	if err != nil {
		return worker.Failed("caller_identity_unavailable", err.Error()), nil
	}
	result, err := e.dispatch(delegated, request)
	var refused *refusal
	switch {
	case errors.As(err, &refused):
		return worker.Failed(refused.kind, refused.message), nil
	case errors.Is(err, catalog.ErrInvalidArguments):
		return worker.Failed("invalid_arguments", err.Error()), nil
	case err != nil:
		return store.ExecutionOutcome{}, err
	default:
		return worker.Succeeded(result, nil)
	}
}

func (e *Executor) delegatedContext(ctx context.Context, request store.ExecutionRequest) (context.Context, error) {
	if request.CallerUID == nil || strings.TrimSpace(*request.CallerUID) == "" {
		return nil, errors.New("the admitted caller ServiceAccount UID was not recorded")
	}
	if request.Caller.Namespace == "" || request.Caller.Name == "" || request.RequestID == uuid.Nil || request.ExecutionID == uuid.Nil {
		return nil, errors.New("the admitted caller or Action correlation identity is incomplete")
	}
	envelope := &delegationpb.Delegation{
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: request.Caller.Namespace, Name: request.Caller.Name},
			Uid:     *request.CallerUID,
		},
		ActionRequestId:   request.RequestID.String(),
		ActionExecutionId: request.ExecutionID.String(),
	}
	return workloadgrpc.WithDelegation(ctx, envelope)
}

func (e *Executor) dispatch(ctx context.Context, request store.ExecutionRequest) (any, error) {
	switch request.Action.Name {
	case contract.ActionListEnvironments:
		args, err := e.inputs.ListEnvironments.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		var pageToken string
		if args.After != nil {
			pageToken = *args.After
		}
		response, err := e.discovery.ListEnvironments(ctx, &discoverypb.ListEnvironmentsRequest{Limit: uint32(args.Limit), PageToken: pageToken})
		if err != nil {
			return nil, readFailure("Environment Discovery", err)
		}
		return protoResult(response)
	case "get":
		switch e.group {
		case contract.GroupEnvironments:
			return e.getEnvironment(ctx, request)
		case contract.GroupSessions:
			return e.getSession(ctx, request)
		default:
			return nil, refuse("unknown_action", "this executor does not expose %q", request.Action.Name)
		}
	case contract.ActionOpenSession:
		args, err := e.inputs.OpenSession.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		if err := validateTargetNamespace(request.Caller.Namespace, args.Target); err != nil {
			return nil, err
		}
		harness, err := args.Spec.Harness.Proto()
		if err != nil {
			return nil, refuse("invalid_arguments", "%s", err)
		}
		if !strings.HasPrefix(args.Spec.Cwd, "/") {
			return nil, refuse("invalid_arguments", "spec.cwd must be an absolute path")
		}
		response, err := e.dispatcher.OpenSession(ctx, &dispatcherpb.OpenSessionRequest{
			SessionId: args.SessionID, Sandbox: args.Target.Name, SandboxUid: args.Target.UID,
			SetupScript: args.SetupScript,
			Spec: &sessionpb.SessionSpec{
				Harness: harness, Model: args.Spec.Model,
				Cwd:             strings.ReplaceAll(args.Spec.Cwd, "{session_id}", args.SessionID),
				ReasoningEffort: args.Spec.ReasoningEffort, Instructions: args.AdditionalInstructions,
			},
		})
		if err != nil {
			return nil, writeFailure("Session Dispatcher", err)
		}
		return protoEnvelope("opening", response.GetOpening())
	case contract.ActionSubmitInput:
		args, err := e.inputs.SubmitInput.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		if err := validateTargetNamespace(request.Caller.Namespace, args.Target); err != nil {
			return nil, err
		}
		response, err := e.dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
			CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Target.Name,
			SandboxUid: args.Target.UID, Text: args.Text,
		})
		if err != nil {
			return nil, writeFailure("Session Dispatcher", err)
		}
		return protoEnvelope("dispatch", response.GetDispatch())
	case contract.ActionObserveInput:
		args, err := e.inputs.ObserveInput.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		result, err := sessionclient.ObserveInput(ctx, e.dispatcher, e.history, sessionclient.ObserveInputRequest{
			SessionID: args.SessionID, CommandID: args.CommandID, WaitSeconds: args.WaitSeconds, ResumeToken: args.ResumeToken,
		})
		if err != nil {
			if status.Code(err) == codes.NotFound {
				result.Dispatch.Kind = "not_observed"
				return result, nil
			}
			// Dispatcher lookup errors are source facts. History errors are captured separately inside
			// ObserveInput's observation projection and return without entering this error path.
			result.Dispatch.Kind = "partial"
			result.Dispatch.Error = sanitizedSourceError(err)
			return result, nil
		}
		return result, nil
	case contract.ActionReadTurns:
		args, err := e.inputs.ReadTurns.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		sessionID, err := uuid.Parse(args.SessionID)
		if err != nil {
			return nil, refuse("invalid_arguments", "session_id must be a UUID")
		}
		var pageCursor string
		if args.PageCursor != nil {
			pageCursor = *args.PageCursor
		}
		page, err := sessionclient.ReadTurns(ctx, e.history, sessionclient.ReadTurnsRequest{
			SessionID: sessionID, Count: uint32(args.Count), PageCursor: pageCursor,
		})
		if err != nil {
			if status.Code(err) == codes.InvalidArgument {
				return nil, refuse("invalid_arguments", "History rejected the Session page cursor or count")
			}
			// ReadTurns can return already projected turns alongside a context error. Its coverage
			// error belongs to History, so attach it there and keep the partial page.
			page.Error = sanitizedSourceError(err)
		}
		return page, nil
	case contract.ActionCancelInput:
		args, err := e.inputs.CancelInput.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		receipt, err := e.dispatcher.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: args.CommandID})
		if err != nil {
			if status.Code(err) == codes.NotFound {
				return nil, refuse("input_not_found", "no input command exists for this command ID")
			}
			return nil, readFailure("Session Dispatcher", err)
		}
		dispatch := receipt.GetDispatch()
		if dispatch == nil || dispatch.GetCommandId() != args.CommandID || dispatch.GetSessionId() != args.SessionID {
			return nil, refuse("input_not_found", "no input command belongs to the supplied Session")
		}
		if dispatch.GetOperation() != dispatcherpb.Operation_OPERATION_SUBMIT_INPUT {
			return nil, refuse("not_input", "command_id does not identify submitted input")
		}
		cancelled, err := e.dispatcher.CancelDispatch(ctx, &dispatcherpb.CancelDispatchRequest{CommandId: args.CommandID})
		if err != nil {
			return nil, writeFailure("Session Dispatcher", err)
		}
		return protoEnvelope("dispatch", cancelled.GetDispatch())
	case contract.ActionInterruptTurn:
		args, err := e.inputs.InterruptTurn.Decode(request.Arguments)
		if err != nil {
			return nil, err
		}
		if err := validateTargetNamespace(request.Caller.Namespace, args.Target); err != nil {
			return nil, err
		}
		response, err := e.dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
			CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Target.Name,
			SandboxUid: args.Target.UID, TurnId: args.TurnID,
		})
		if err != nil {
			return nil, writeFailure("Session Dispatcher", err)
		}
		return protoResult(response)
	default:
		return nil, refuse("unknown_action", "this executor does not expose %q", request.Action.Name)
	}
}

func (e *Executor) getEnvironment(ctx context.Context, request store.ExecutionRequest) (any, error) {
	args, err := e.inputs.GetEnvironment.Decode(request.Arguments)
	if err != nil {
		return nil, err
	}
	response, err := e.discovery.GetEnvironment(ctx, &discoverypb.GetEnvironmentRequest{Target: &discoverypb.TargetHandle{
		Namespace: args.Target.Namespace, Name: args.Target.Name, Uid: args.Target.UID,
	}})
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, refuse("target_not_found", "the exact environment incarnation is no longer available")
		}
		return nil, readFailure("Environment Discovery", err)
	}
	return protoResult(response)
}

func (e *Executor) getSession(ctx context.Context, request store.ExecutionRequest) (any, error) {
	args, err := e.inputs.GetSession.Decode(request.Arguments)
	if err != nil {
		return nil, err
	}
	sessionID, err := uuid.Parse(args.SessionID)
	if err != nil {
		return nil, refuse("invalid_arguments", "session_id must be a UUID")
	}
	snapshot, err := sessionclient.GetSession(ctx, e.dispatcher, e.history, sessionID)
	if err != nil {
		// GetSession may return facts read before the context ended. Keep those facts and report
		// the aggregate interruption without misattributing it to either source.
		return sessionSnapshotResult{SessionSnapshot: snapshot, ReadError: sanitizedSourceError(err)}, nil
	}
	return sessionSnapshotResult{SessionSnapshot: snapshot}, nil
}

type sessionSnapshotResult struct {
	sessionclient.SessionSnapshot
	ReadError *sessionclient.SourceError `json:"read_error,omitempty"`
}

func validateTargetNamespace(callerNamespace string, target contract.TargetRef) error {
	if target.Namespace != callerNamespace {
		return refuse("invalid_arguments", "target.namespace must match the authenticated caller namespace")
	}
	return nil
}

type refusal struct{ kind, message string }

func (r *refusal) Error() string { return r.message }

func refuse(kind, format string, args ...any) error {
	return &refusal{kind: kind, message: fmt.Sprintf(format, args...)}
}

func protoResult(message proto.Message) (json.RawMessage, error) {
	if message == nil {
		return nil, errors.New("backend returned no result message")
	}
	return protojson.MarshalOptions{UseProtoNames: true}.Marshal(message)
}

func protoEnvelope(field string, message proto.Message) (json.RawMessage, error) {
	encoded, err := protoResult(message)
	if err != nil {
		return nil, err
	}
	return json.Marshal(map[string]json.RawMessage{field: encoded})
}

// readFailure is safe to return as a failed Action because reads do not mutate backend state.
func readFailure(source string, err error) error {
	return refuse("source_unavailable", "%s read failed: %s", source, status.Convert(err).Code())
}

func sanitizedSourceError(err error) *sessionclient.SourceError {
	code := status.Code(err)
	if code == codes.Unknown {
		code = codes.Unavailable
	}
	message := "service unavailable"
	switch code {
	case codes.Canceled:
		message = "read canceled"
	case codes.DeadlineExceeded:
		message = "read deadline expired"
	case codes.NotFound:
		message = "not found"
	case codes.PermissionDenied, codes.Unauthenticated:
		message = "permission denied"
	case codes.InvalidArgument:
		message = "invalid request"
	case codes.DataLoss:
		message = "retained history has a gap"
	}
	return &sessionclient.SourceError{Code: code.String(), Message: message}
}

// writeFailure distinguishes explicit backend refusals from transport/deadline errors whose mutation outcome is unknown.
func writeFailure(source string, err error) error {
	converted := status.Convert(err)
	switch converted.Code() {
	case codes.InvalidArgument:
		return refuse("invalid_arguments", "%s rejected the request", source)
	case codes.NotFound:
		return refuse("target_not_found", "%s could not find the requested resource", source)
	case codes.AlreadyExists:
		return refuse("id_reused", "%s reports that a caller ID was used for different work", source)
	case codes.PermissionDenied, codes.Unauthenticated:
		return refuse("backend_refused", "%s refused the delegated caller", source)
	case codes.FailedPrecondition:
		return refuse("rejected", "%s rejected the current resource state", source)
	case codes.ResourceExhausted:
		return refuse("capacity_exhausted", "%s could not accept the request", source)
	default:
		return fmt.Errorf("%w: %s call outcome is unknown: %w", worker.ErrOutcomeUnknown, source, err)
	}
}
