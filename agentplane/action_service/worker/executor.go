package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
)

// Executor runs Executions on one backend.
type Executor interface {
	// Execute makes the one attempt at request and returns how it ended: succeeded with a result, or failed with an
	// error document. An error wrapping ErrOutcomeUnknown says the attempt cannot tell whether the effect happened;
	// any other error fails the Execution with the error's type, never its message, as its kind.
	//
	// Execute holds lease while it works, with Hold or Lease.Renew, and bounds its own work: renewal proves the
	// executor alive, not that the backend progresses. It returns promptly once ctx ends, which a drain does at its
	// deadline.
	Execute(ctx context.Context, request store.ExecutionRequest, lease *Lease) (store.ExecutionOutcome, error)
}

// Executors routes each Action to the Executor that runs it.
type Executors interface {
	// Route returns the Executor that runs the Action, nil when none does, and whether that Executor takes an
	// Execution now. An Execution whose Executor does not take one now is left awaiting dispatch until a wakeup
	// finds it ready; one that no Executor runs fails. Route is called inside the claiming transaction: it must not
	// block or use the database.
	Route(action store.ActionIdentity) (executor Executor, ready bool)
}

// ErrOutcomeUnknown is an attempt that cannot tell whether its effect happened. Its Execution ends as
// execution_unknown and is never replayed; a later outcome under the same lease still reconciles it.
var ErrOutcomeUnknown = errors.New("execution outcome unknown")

// UnsupportedActionError fails an Execution whose Action no ready Executor runs when it starts.
type UnsupportedActionError struct {
	Action store.ActionIdentity
}

func (e *UnsupportedActionError) Error() string {
	return fmt.Sprintf("no executor runs Action %s/%s", e.Action.Group, e.Action.Name)
}

// InvalidOutcomeError fails an Execution whose Executor answered an outcome the store does not take.
type InvalidOutcomeError struct {
	Err error
}

func (e *InvalidOutcomeError) Error() string { return "invalid execution outcome: " + e.Err.Error() }

// The outcomes the worker records itself carry the error documents the Python service writes.
const (
	stoppedMessage = "dispatch outcome unknown; not replayed"
	unknownMessage = "execution outcome is unknown; not replayed"
	failedMessage  = "executor failed; see the Action Service log"
)

func unknownOutcome(reason store.UnknownOutcomeReason, message string) store.ExecutionOutcome {
	return ErrorDocument{Kind: string(reason), Message: message}.Outcome(store.ExecutionUnknown)
}

// failedOutcome names err by its type, as the Python service names an exception by its class: the message can
// carry request or backend material, so only the log has it.
func failedOutcome(err error) store.ExecutionOutcome {
	return Failed(fmt.Sprintf("%T", err), failedMessage)
}

// ErrorDocument is the error of an Execution that failed or whose outcome is unknown, as the Python service writes it.
type ErrorDocument struct {
	// Kind names why, for a program; Message says it to the caller.
	Kind    string `json:"kind"`
	Message string `json:"message"`
	// Diagnostics are what an MCP group's failed request showed, when it showed any.
	Diagnostics map[string]any `json:"diagnostics,omitempty"`
}

// Outcome is an Execution that ended in state with d as its error.
func (d ErrorDocument) Outcome(state store.ExecutionState) store.ExecutionOutcome {
	encoded, err := json.Marshal(d)
	if err != nil {
		panic(err)
	}
	return store.ExecutionOutcome{State: state, Error: encoded}
}

// Failed is an Execution that failed with the error kind and message.
func Failed(kind, message string) store.ExecutionOutcome {
	return ErrorDocument{Kind: kind, Message: message}.Outcome(store.ExecutionFailed)
}

// Succeeded is an Execution that succeeded with result as its JSON, or err when the attempt producing it failed.
func Succeeded[T any](result T, err error) (store.ExecutionOutcome, error) {
	if err != nil {
		return store.ExecutionOutcome{}, err
	}
	document, err := json.Marshal(result)
	if err != nil {
		return store.ExecutionOutcome{}, err
	}
	return store.ExecutionOutcome{State: store.ExecutionSucceeded, Result: document}, nil
}
