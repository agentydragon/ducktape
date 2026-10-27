package orchestrationv1

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

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

// sessionSpec is a Session's SessionSpec, its harness by the enum value's name.
type sessionSpec struct {
	Harness         string `json:"harness"`
	Model           string `json:"model"`
	Cwd             string `json:"cwd"`
	ReasoningEffort string `json:"reasoning_effort,omitempty"`
	Instructions    string `json:"instructions,omitempty"`
}

type openSessionArgs struct {
	Sandbox     SandboxRef  `json:"sandbox"`
	SessionID   string      `json:"session_id"`
	Spec        sessionSpec `json:"spec"`
	SetupScript *string     `json:"setup_script"`
}

// commandArgs are the arguments of each Session command, which takes one of the last four by its Action.
type commandArgs struct {
	Sandbox   SandboxRef `json:"sandbox"`
	SessionID string     `json:"session_id"`
	CommandID string     `json:"command_id"`
	Text      string     `json:"text"`
	TurnID    string     `json:"turn_id"`
	Model     string     `json:"model"`
	Effort    string     `json:"effort"`
}

// openSession queues the Session's opening with the Session Dispatcher and answers with it. The app opens Sessions the
// same way, and as it does, `{session_id}` in the working directory becomes the Session's ID.
func (e *Executor) openSession(ctx context.Context, args openSessionArgs) (json.RawMessage, error) {
	if err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	response, err := e.dispatcher.OpenSession(ctx, &dispatcherpb.OpenSessionRequest{
		SessionId: args.SessionID, Sandbox: args.Sandbox.Name, SetupScript: args.SetupScript,
		Spec: &runnerpb.SessionSpec{
			Harness: runnerpb.Harness(runnerpb.Harness_value[args.Spec.Harness]), Model: args.Spec.Model,
			Cwd:             strings.ReplaceAll(args.Spec.Cwd, "{session_id}", args.SessionID),
			ReasoningEffort: args.Spec.ReasoningEffort, Instructions: args.Spec.Instructions,
		},
	})
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(response.GetOpening())
}

// command sends the Session command action names and answers with its Dispatch, or the interrupt's outcome.
func (e *Executor) command(ctx context.Context, action string, args commandArgs) (json.RawMessage, error) {
	if err := e.requireSandbox(ctx, args.Sandbox); err != nil {
		return nil, err
	}
	command, session, sandbox, uid := args.CommandID, args.SessionID, args.Sandbox.Name, string(args.Sandbox.UID)
	var answer proto.Message
	var err error
	switch action {
	case actionSubmitInput:
		var response *dispatcherpb.SubmitInputResponse
		response, err = e.dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
			CommandId: command, SessionId: session, Sandbox: sandbox, SandboxUid: uid, Text: args.Text,
		})
		answer = response.GetDispatch()
	case actionInterruptTurn:
		answer, err = e.dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
			CommandId: command, SessionId: session, Sandbox: sandbox, SandboxUid: uid, TurnId: args.TurnID,
		})
	case actionChangeModel:
		var response *dispatcherpb.ChangeModelResponse
		response, err = e.dispatcher.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{
			CommandId: command, SessionId: session, Sandbox: sandbox, SandboxUid: uid, Model: args.Model,
		})
		answer = response.GetDispatch()
	case actionChangeReasoningEffort:
		var response *dispatcherpb.ChangeReasoningEffortResponse
		response, err = e.dispatcher.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{
			CommandId: command, SessionId: session, Sandbox: sandbox, SandboxUid: uid, Effort: args.Effort,
		})
		answer = response.GetDispatch()
	default:
		panic(fmt.Sprintf("orchestrationv1: %q is no Session command", action))
	}
	if err != nil {
		return nil, dispatcherError(err)
	}
	return protoResult(answer)
}

// requireSandbox refuses a command for a Sandbox that is gone or is another incarnation than the one named. A grant
// is pinned to one incarnation, and OpenSession takes the Sandbox by name alone, so this is what keeps it from reaching
// a same-name replacement.
func (e *Executor) requireSandbox(ctx context.Context, ref SandboxRef) error {
	var sandbox sandboxapi.Sandbox
	err := e.kube.Get(ctx, client.ObjectKey{Namespace: e.binding.Namespace, Name: ref.Name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
		return refuse("sandbox_not_found", "no Sandbox named %q exists", ref.Name)
	case err != nil:
		return kubeError("reading the Sandbox", err)
	case sandbox.UID != ref.UID:
		return refuse("sandbox_replaced", "Sandbox %q is no longer the incarnation %s", ref.Name, ref.UID)
	}
	return nil
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
