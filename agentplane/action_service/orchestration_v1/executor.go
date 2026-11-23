package orchestrationv1

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/worker"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Executor runs one orchestration_v1 group's Actions in this process. It acts with this service's own authority over
// Sandboxes, PolicySets, the Session Dispatcher and the History Service, which a request reaches only once it is decided: the Decision, the
// operator's or a rule's, is what authorizes it. Launcher grants are bound to ExecutionRequest.Caller, which the
// authenticated admission path sets and no argument can reach.
type Executor struct {
	// group is the catalog key the group is configured under, which the launcher grants' rules name.
	group       string
	binding     catalog.OrchestrationBinding
	presets     map[string]Preset
	kube        client.Client
	environment environmentpb.EnvironmentServiceClient
	dispatcher  dispatcherpb.SessionDispatcherClient
	history     historypb.HistoryServiceClient
	inputs      contract.Inputs
	readBudget  time.Duration
}

// NewExecutor is the Executor of the group under key. kube reaches binding.Namespace with a scheme that holds Sandboxes,
// PolicySets and PolicyBindings.
func NewExecutor(
	group string, binding catalog.OrchestrationBinding, kube client.Client, environment environmentpb.EnvironmentServiceClient, dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient, directWait time.Duration,
) (*Executor, error) {
	presets, err := parsePresets(binding.Presets)
	if err != nil {
		return nil, err
	}
	inputs, err := contract.NewInputs()
	if err != nil {
		return nil, err
	}
	return &Executor{
		group: group, binding: binding, presets: presets, kube: kube, environment: environment, dispatcher: dispatcher, history: history,
		inputs: inputs, readBudget: directWait - time.Second,
	}, nil
}

// Actions are the Actions this executor runs, with the same input contracts it decodes.
func (e *Executor) Actions() map[string]catalog.ActionDefinition { return e.inputs.Actions() }

// refusal is an attempt refused for a reason its caller can act on, before or without any effect.
type refusal struct {
	kind    action_servicepb.ExecutionErrorKind
	message string
}

func (r *refusal) Error() string { return r.message }

func refuse(kind action_servicepb.ExecutionErrorKind, format string, args ...any) error {
	return &refusal{kind: kind, message: fmt.Sprintf(format, args...)}
}

// Execute makes one attempt at request, holding the lease for as long as the attempt takes: a few API server, Session
// Dispatcher and History Service calls, each bounded by its client.
func (e *Executor) Execute(ctx context.Context, request store.ExecutionRequest, lease *worker.Lease) (store.ExecutionOutcome, error) {
	return worker.Hold(ctx, lease, func(ctx context.Context) (store.ExecutionOutcome, error) {
		return e.attempt(ctx, request)
	})
}

// attempt is one attempt at request. A refusal is a failed Execution with its reason; only an outcome this cannot
// characterise is an error.
func (e *Executor) attempt(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	outcome, err := e.dispatch(ctx, request)
	var refused *refusal
	switch {
	case errors.As(err, &refused):
		return worker.Failed(refused.kind, refused.message), nil
	case errors.Is(err, catalog.ErrInvalidArguments):
		return worker.Failed(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INVALID_ARGUMENTS, err.Error()), nil
	}
	return outcome, err
}

func (e *Executor) dispatch(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	action := request.Action.Name
	switch action {
	case contract.ActionListLaunchPresets:
		return run(ctx, request, e.inputs.ListLaunchPresets, func(context.Context, contract.NoArgs) (PresetList, error) {
			return PresetList{Presets: e.presets}, nil
		})
	case contract.ActionLaunchSandbox:
		return run(ctx, request, e.inputs.LaunchSandbox, func(ctx context.Context, args contract.LaunchArgs) (Launched, error) {
			return e.launch(ctx, request.Caller, args)
		})
	case contract.ActionCreateSession:
		return run(ctx, request, e.inputs.CreateSession, e.createSession)
	case contract.ActionSubmitInput:
		return run(ctx, request, e.inputs.SubmitInput, e.submitInput)
	case contract.ActionInterruptTurn:
		return run(ctx, request, e.inputs.InterruptTurn, e.interruptTurn)
	case contract.ActionChangeModel:
		return run(ctx, request, e.inputs.ChangeModel, e.changeModel)
	case contract.ActionChangeReasoningEffort:
		return run(ctx, request, e.inputs.ChangeReasoningEffort, e.changeReasoningEffort)
	case contract.ActionReadTurns:
		return run(ctx, request, e.inputs.ReadTurns, e.readTurns)
	case contract.ActionWaitForTurn:
		return run(ctx, request, e.inputs.WaitForTurn, e.waitForTurn)
	case contract.ActionGetSession:
		return run(ctx, request, e.inputs.GetSession, e.getSession)
	case contract.ActionListSessions:
		return run(ctx, request, e.inputs.ListSessions, e.listSessions)
	default:
		// The catalog admitted it, so this is a roster that grew without a branch here.
		return worker.Failed(action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_UNKNOWN_ACTION, fmt.Sprintf("this executor has no %q", action)), nil
	}
}

// run decodes the request's arguments with input and succeeds with what act answers for them.
func run[Args, Result any](
	ctx context.Context, request store.ExecutionRequest, input catalog.Input[Args], act func(context.Context, Args) (Result, error),
) (store.ExecutionOutcome, error) {
	args, err := input.Decode(request.Arguments)
	if err != nil {
		return store.ExecutionOutcome{}, err
	}
	return worker.Succeeded(act(ctx, args))
}
