package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/santhosh-tekuri/jsonschema/v6"
)

// Executor runs the sandbox Actions in this process, as the caller the request names. That identity comes from
// ExecutionRequest.Caller, which the authenticated admission path sets and no argument can reach: a surface that
// took the account as an argument would instead mean "stamp a Pod as any account you can name".
//
// A box outlives any one call and is its caller's to dispose of, so a drain leaves boxes alone.
type Executor struct {
	binding   catalog.PodBinding
	inventory *Inventory
	// arguments are the schemas of each Action's arguments, without this group's caps: a request admitted under
	// other caps is cut to these, not refused.
	arguments map[string]*jsonschema.Schema
}

func NewExecutor(binding catalog.PodBinding, inventory *Inventory) (*Executor, error) {
	arguments := map[string]*jsonschema.Schema{}
	for action, schema := range schemas() {
		compiled, err := catalog.CompileSchema(schema)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", action, err)
		}
		arguments[action] = compiled
	}
	return &Executor{binding: binding, inventory: inventory, arguments: arguments}, nil
}

// Execute makes one attempt at request, holding the lease for as long as the attempt takes, which the attempt
// bounds: an exec ends a little after its script's timeout, so renewal can outlast a lease window, as a clone of a
// large repository does, but never runs unbounded. A caller no box can be given is refused before the lease is held:
// nothing has happened, so the outcome is known.
func (e *Executor) Execute(ctx context.Context, request store.ExecutionRequest, lease *worker.Lease) (store.ExecutionOutcome, error) {
	if caller := request.Caller; caller.Namespace != e.binding.Namespace {
		// A Pod can only run as a ServiceAccount of its own namespace, so a caller from anywhere else cannot be given
		// a box that is it. Stamping the box as some other account would quietly hand it authority its caller never
		// had.
		return worker.Failed("caller_not_local", fmt.Sprintf("sandboxes run as their caller, so the calling ServiceAccount "+
			"must live in %q; %q cannot be given one", e.binding.Namespace, caller.Namespace)), nil
	}
	return worker.Hold(ctx, lease, func(ctx context.Context) (store.ExecutionOutcome, error) {
		return e.attempt(ctx, request)
	})
}

// attempt is one attempt at request from a caller of the group's namespace. A refusal the caller can act on is a
// failed Execution with its reason; only an outcome this cannot characterise is an error.
func (e *Executor) attempt(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	outcome, err := e.dispatch(ctx, request)
	var foreign *ForeignError
	var unavailable *UnavailableError
	var unrun *ExecError
	switch {
	case errors.As(err, &foreign):
		return worker.Failed("sandbox_not_yours", err.Error()), nil
	case errors.As(err, &unavailable):
		return worker.Failed("sandbox_unavailable", err.Error()), nil
	case errors.As(err, &unrun):
		return worker.Failed("exec_failed", err.Error()), nil
	case errors.Is(err, catalog.ErrInvalidArguments):
		return worker.Failed("invalid_arguments", err.Error()), nil
	}
	return outcome, err
}

type createArgs struct {
	Name     string `json:"name"`
	Template string `json:"template"`
}

type templateArgs struct {
	Template string `json:"template"`
}

type execArgs struct {
	Name   string  `json:"name"`
	Script string  `json:"script"`
	Cwd    *string `json:"cwd"`
	// The integers decode as float64 because a JSON Schema integer may be written 60.0; the schema bounds them.
	TimeoutSeconds float64 `json:"timeout_seconds"`
	MaxOutputBytes float64 `json:"max_output_bytes"`
}

type nameArgs struct {
	Name string `json:"name"`
}

// SandboxList is the result of list.
type SandboxList struct {
	Sandboxes []Info `json:"sandboxes"`
}

// DisposeResult is the result of dispose; disposing an absent box is not an error.
type DisposeResult struct {
	Name    string `json:"name"`
	Existed bool   `json:"existed"`
}

func (e *Executor) dispatch(ctx context.Context, request store.ExecutionRequest) (store.ExecutionOutcome, error) {
	caller, action := request.Caller, request.Action.Name
	switch action {
	case actionCreate:
		var args createArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.inventory.Create(ctx, caller, args.Name, args.Template))
	case actionGetTemplate:
		var args templateArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.inventory.Template(ctx, args.Template))
	case actionExec:
		var args execArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.inventory.Run(ctx, caller, args.Name, Script{
			Text: args.Script, Cwd: args.Cwd, TimeoutSeconds: int(args.TimeoutSeconds), MaxOutputBytes: int(args.MaxOutputBytes),
		}))
	case actionList:
		if err := e.decode(action, request.Arguments, &struct{}{}); err != nil {
			return store.ExecutionOutcome{}, err
		}
		boxes, err := e.inventory.List(ctx, caller)
		return worker.Succeeded(SandboxList{Sandboxes: boxes}, err)
	case actionGet:
		var args nameArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		return worker.Succeeded(e.inventory.Get(ctx, caller, args.Name))
	case actionDispose:
		var args nameArgs
		if err := e.decode(action, request.Arguments, &args); err != nil {
			return store.ExecutionOutcome{}, err
		}
		existed, err := e.inventory.Dispose(ctx, caller, args.Name)
		return worker.Succeeded(DisposeResult{Name: args.Name, Existed: existed}, err)
	default:
		// The catalog admitted it, so this is a roster that grew without a branch here.
		return worker.Failed("unknown_action", fmt.Sprintf("this executor has no %q", action)), nil
	}
}

// decode reads arguments into args once they match the Action's schema.
func (e *Executor) decode(action string, arguments json.RawMessage, args any) error {
	return catalog.DecodeArguments(e.arguments[action], arguments, args)
}
