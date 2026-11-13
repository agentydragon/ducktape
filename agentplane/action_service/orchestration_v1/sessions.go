package orchestrationv1

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// createSession asks Dispatcher to create a Session and returns its current resource state.
// As in App creation, `{session_id}` in the working directory becomes the Session's ID.
func (e *Executor) createSession(ctx context.Context, args contract.CreateSessionArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: args.SessionID, SetupScript: args.SetupScript,
		Spec: &sessionpb.SessionSpec{
			Model:           args.Spec.Model,
			Cwd:             strings.ReplaceAll(args.Spec.Cwd, "{session_id}", args.SessionID),
			ReasoningEffort: args.Spec.ReasoningEffort, Instructions: args.Spec.Instructions,
		},
	}
	target := &sessionpb.ExactEnvironment{Name: args.Sandbox.Name, Uid: string(args.Sandbox.UID)}
	harness := sessionpb.Harness(sessionpb.Harness_value[string(args.Spec.Harness)])
	if harness == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
		request.Spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: target}}}
	} else {
		request.Spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: target, Harness: harness}}}
	}
	response, err := e.dispatcher.CreateSession(ctx, request)
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetSession())
}

// submitInput queues user text for a Session and answers with its Dispatch.
func (e *Executor) submitInput(ctx context.Context, args contract.SubmitInputArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, ExpectedSandboxUid: string(args.Sandbox.UID), Text: args.Text,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetDispatch())
}

// interruptTurn queues an exact-turn interrupt and answers with its Dispatch.
func (e *Executor) interruptTurn(ctx context.Context, args contract.InterruptTurnArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	answer, err := e.dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, ExpectedSandboxUid: string(args.Sandbox.UID), TurnId: args.TurnID,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(answer.GetDispatch())
}

// changeModel queues a Session model change and answers with its Dispatch.
func (e *Executor) changeModel(ctx context.Context, args contract.ChangeModelArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, ExpectedSandboxUid: string(args.Sandbox.UID), Model: args.Model,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetDispatch())
}

// changeReasoningEffort queues a Session reasoning-effort change and answers with its Dispatch.
func (e *Executor) changeReasoningEffort(ctx context.Context, args contract.ChangeReasoningEffortArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, ExpectedSandboxUid: string(args.Sandbox.UID), Effort: args.Effort,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetDispatch())
}

// requireSandbox reads the named incarnation, refusing a Sandbox that is gone or is another incarnation. A grant is
// pinned to one incarnation; commands and sessions pass its UID to the Dispatcher, which checks the current UID too.
func (e *Executor) requireSandbox(ctx context.Context, ref contract.SandboxRef) (*sandboxapi.Sandbox, error) {
	var sandbox sandboxapi.Sandbox
	err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: ref.Name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
		return nil, refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_FOUND, "no Sandbox named %q exists", ref.Name)
	case err != nil:
		return nil, kubeError("reading the Sandbox", err)
	case sandbox.UID != ref.UID:
		return nil, refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_REPLACED, "Sandbox %q is no longer the incarnation %s", ref.Name, ref.UID)
	}
	return &sandbox, nil
}

// dispatcherError is err from the Session Dispatcher as the attempt's outcome. A call the Dispatcher refused, or one
// never sent, had no effect and is a refusal; any other failure leaves the outcome unknown.
func dispatcherError(err error) error {
	if bearer.IsTokenFileError(err) {
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_DISPATCHER_UNREACHABLE, "the Session Dispatcher was not called: %v", err)
	}
	refused := status.Convert(err)
	switch refused.Code() {
	case codes.NotFound:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_SANDBOX_NOT_FOUND, "%s", refused.Message())
	case codes.AlreadyExists:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_ID_REUSED, "%s", refused.Message())
	case codes.InvalidArgument:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INVALID_ARGUMENTS, "%s", refused.Message())
	case codes.FailedPrecondition:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_REJECTED, "%s", refused.Message())
	case codes.PermissionDenied, codes.Unauthenticated:
		return refuse(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_DISPATCHER_REFUSED, "%s", refused.Message())
	}
	return fmt.Errorf("%w: Session Dispatcher: %w", worker.ErrOutcomeUnknown, err)
}

// protoResult is message as an Action result, a JSON object with the proto field names.
func protoResult(message proto.Message) (json.RawMessage, error) {
	return protojson.MarshalOptions{UseProtoNames: true}.Marshal(message)
}
