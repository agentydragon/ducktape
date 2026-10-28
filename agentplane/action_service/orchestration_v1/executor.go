package orchestrationv1

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Executor runs one orchestration_v1 group's Actions in this process. It acts with this service's own authority over
// Sandboxes, PolicySets, the Session Dispatcher and the History Service, which a request reaches only once it is decided: the Decision, the
// operator's or a rule's, is what authorizes it. Launcher grants are bound to ExecutionRequest.Caller, which the
// authenticated admission path sets and no argument can reach.
type Executor struct {
	// group is the catalog key the group is configured under, which the launcher grants' rules name.
	group      string
	binding    catalog.OrchestrationBinding
	presets    map[string]Preset
	kube       client.Client
	dispatcher dispatcherpb.SessionDispatcherClient
	history    historypb.HistoryServiceClient
	inputs     contract.Inputs
	readBudget time.Duration
}

// NewExecutor is the Executor of the group under key. kube reaches binding.Namespace with a scheme that holds Sandboxes,
// PolicySets and PolicyBindings.
func NewExecutor(
	group string, binding catalog.OrchestrationBinding, kube client.Client, dispatcher dispatcherpb.SessionDispatcherClient,
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
		group: group, binding: binding, presets: presets, kube: kube, dispatcher: dispatcher, history: history,
		inputs: inputs, readBudget: directWait - time.Second,
	}, nil
}

// Actions are the Actions this executor runs, with the same input contracts it decodes.
func (e *Executor) Actions() map[string]catalog.ActionDefinition { return e.inputs.Actions() }

// refusal is an attempt refused for a reason its caller can act on, before or without any effect.
type refusal struct{ kind, message string }

func (r *refusal) Error() string { return r.message }

func refuse(kind, format string, args ...any) error {
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
		return worker.Failed("invalid_arguments", err.Error()), nil
	}
	return outcome, err
}

func (e *Executor) dispatch(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	action := request.Action.Name
	switch action {
	case contract.ActionListLaunchPresets:
		if _, err := e.inputs.ListLaunchPresets.Decode(request.Arguments); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(PresetList{Presets: e.presets}, nil)
	case contract.ActionLaunchSandbox:
		args, err := e.inputs.LaunchSandbox.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.launch(ctx, request.Caller, args))
	case contract.ActionOpenSession:
		args, err := e.inputs.OpenSession.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.openSession(ctx, args))
	case contract.ActionSubmitInput:
		args, err := e.inputs.SubmitInput.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.submitInput(ctx, args))
	case contract.ActionInterruptTurn:
		args, err := e.inputs.InterruptTurn.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.interruptTurn(ctx, args))
	case contract.ActionChangeModel:
		args, err := e.inputs.ChangeModel.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.changeModel(ctx, args))
	case contract.ActionChangeReasoningEffort:
		args, err := e.inputs.ChangeReasoningEffort.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.changeReasoningEffort(ctx, args))
	case contract.ActionReadTurns:
		args, err := e.inputs.ReadTurns.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.readTurns(ctx, args))
	case contract.ActionWaitForTurn:
		args, err := e.inputs.WaitForTurn.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.waitForTurn(ctx, args))
	case contract.ActionGetSession:
		args, err := e.inputs.GetSession.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.getSession(ctx, args))
	case contract.ActionListSessions:
		args, err := e.inputs.ListSessions.Decode(request.Arguments)
		if err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.listSessions(ctx, args))
	default:
		// The catalog admitted it, so this is a roster that grew without a branch here.
		return worker.Failed("unknown_action", fmt.Sprintf("this executor has no %q", action)), nil
	}
}
