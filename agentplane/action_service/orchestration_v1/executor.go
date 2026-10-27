package orchestrationv1

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Executor runs one orchestration_v1 group's Actions in this process. It acts with this service's own authority over
// Sandboxes, PolicySets and the Session Dispatcher, which a request reaches only once it is decided: the Decision, the
// operator's or a rule's, is what authorizes it. Launcher grants are bound to ExecutionRequest.Caller, which the
// authenticated admission path sets and no argument can reach.
type Executor struct {
	// group is the catalog key the group is configured under, which the launcher grants' rules name.
	group      string
	binding    catalog.OrchestrationBinding
	presets    map[string]Preset
	kube       client.Client
	dispatcher dispatcherpb.SessionDispatcherClient
	arguments  map[string]*jsonschema.Schema
}

// NewExecutor is the Executor of the group under key. kube reaches binding.Namespace with a scheme that holds Sandboxes,
// PolicySets and PolicyBindings.
func NewExecutor(group string, binding catalog.OrchestrationBinding, kube client.Client, dispatcher dispatcherpb.SessionDispatcherClient) (*Executor, error) {
	presets, err := parsePresets(binding.Presets)
	if err != nil {
		return nil, err
	}
	arguments := map[string]*jsonschema.Schema{}
	for action, schema := range schemas() {
		compiled, err := catalog.CompileSchema(schema)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", action, err)
		}
		arguments[action] = compiled
	}
	return &Executor{
		group: group, binding: binding, presets: presets, kube: kube, dispatcher: dispatcher, arguments: arguments,
	}, nil
}

// refusal is an attempt refused for a reason its caller can act on, before or without any effect.
type refusal struct{ kind, message string }

func (r *refusal) Error() string { return r.message }

func refuse(kind, format string, args ...any) error {
	return &refusal{kind: kind, message: fmt.Sprintf(format, args...)}
}

// Execute makes one attempt at request, holding the lease for as long as the attempt takes: a few API server and
// Session Dispatcher calls, each bounded by its client.
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
	case actionListLaunchPresets:
		if err := e.decode(action, request.Arguments, &struct{}{}); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(PresetList{Presets: e.presets}, nil)
	case actionLaunchSandbox:
		var args launchArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.launch(ctx, request.Caller, args))
	case actionOpenSession:
		var args openSessionArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.openSession(ctx, args))
	case actionSubmitInput, actionInterruptTurn, actionChangeModel, actionChangeReasoningEffort:
		var args commandArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.command(ctx, action, args))
	default:
		// The catalog admitted it, so this is a roster that grew without a branch here.
		return worker.Failed("unknown_action", fmt.Sprintf("this executor has no %q", action)), nil
	}
}

// decode reads arguments into args once they match the Action's schema.
func (e *Executor) decode(action string, arguments json.RawMessage, args any) error {
	return catalog.DecodeArguments(e.arguments[action], arguments, args)
}
