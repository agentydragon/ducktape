package orchestrationv1

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// openSession queues the Session's opening with the Session Dispatcher and answers with it. The app opens Sessions the
// same way, and as it does, `{session_id}` in the working directory becomes the Session's ID.
func (e *Executor) openSession(ctx context.Context, args contract.OpenSessionArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.OpenSession(ctx, &dispatcherpb.OpenSessionRequest{
		SessionId: args.SessionID, Sandbox: args.Sandbox.Name, SetupScript: args.SetupScript,
		Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness(runnerpb.Harness_value[string(args.Spec.Harness)]), Model: args.Spec.Model,
			Cwd:             strings.ReplaceAll(args.Spec.Cwd, "{session_id}", args.SessionID),
			ReasoningEffort: args.Spec.ReasoningEffort, Instructions: args.Spec.Instructions,
		},
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetOpening())
}

// submitInput queues user text for a Session and answers with its Dispatch.
func (e *Executor) submitInput(ctx context.Context, args contract.SubmitInputArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Sandbox.Name,
		SandboxUid: string(args.Sandbox.UID), Text: args.Text,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetDispatch())
}

// interruptTurn sends a Session interrupt directly to the runner and answers with the admitted result.
func (e *Executor) interruptTurn(ctx context.Context, args contract.InterruptTurnArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	answer, err := e.dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Sandbox.Name,
		SandboxUid: string(args.Sandbox.UID), TurnId: args.TurnID,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(answer)
}

// changeModel queues a Session model change and answers with its Dispatch.
func (e *Executor) changeModel(ctx context.Context, args contract.ChangeModelArgs) (json.RawMessage, error) {
	if _, err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{
		CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Sandbox.Name,
		SandboxUid: string(args.Sandbox.UID), Model: args.Model,
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
		CommandId: args.CommandID, SessionId: args.SessionID, Sandbox: args.Sandbox.Name,
		SandboxUid: string(args.Sandbox.UID), Effort: args.Effort,
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetDispatch())
}

// requireSandbox reads the named incarnation, refusing a Sandbox that is gone or is another incarnation. A grant is
// pinned to one incarnation, and OpenSession takes the Sandbox by name alone, so this is what keeps a call from reaching
// a same-name replacement.
func (e *Executor) requireSandbox(ctx context.Context, ref contract.SandboxRef) (*sandboxapi.Sandbox, error) {
	var sandbox sandboxapi.Sandbox
	err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: ref.Name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
		return nil, refuse("sandbox_not_found", "no Sandbox named %q exists", ref.Name)
	case err != nil:
		return nil, kubeError("reading the Sandbox", err)
	case sandbox.UID != ref.UID:
		return nil, refuse("sandbox_replaced", "Sandbox %q is no longer the incarnation %s", ref.Name, ref.UID)
	}
	return &sandbox, nil
}

// dispatcherError is err from the Session Dispatcher as the attempt's outcome. A call the Dispatcher refused, or one
// never sent, had no effect and is a refusal; any other failure leaves the outcome unknown.
func dispatcherError(err error) error {
	if bearer.IsTokenFileError(err) {
		return refuse("dispatcher_unreachable", "the Session Dispatcher was not called: %v", err)
	}
	refused := status.Convert(err)
	switch refused.Code() {
	case codes.NotFound:
		return refuse("sandbox_not_found", "%s", refused.Message())
	case codes.AlreadyExists:
		return refuse("id_reused", "%s", refused.Message())
	case codes.InvalidArgument:
		return refuse("invalid_arguments", "%s", refused.Message())
	case codes.FailedPrecondition:
		return refuse("rejected", "%s", refused.Message())
	case codes.PermissionDenied, codes.Unauthenticated:
		return refuse("dispatcher_refused", "%s", refused.Message())
	}
	return fmt.Errorf("%w: Session Dispatcher: %w", worker.ErrOutcomeUnknown, err)
}

// protoResult is message as an Action result, a JSON object with the proto field names.
func protoResult(message proto.Message) (json.RawMessage, error) {
	return protojson.MarshalOptions{UseProtoNames: true}.Marshal(message)
}
