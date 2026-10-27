package api

import (
	"encoding/json"
	"fmt"
	"math"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// The views turn what the store reads into the messages the API answers with. A stored request no message can
// show fails with errStoredRequest.

var executionStates = map[store.ExecutionState]action_servicepb.ExecutionState{
	store.ExecutionPendingDispatch: action_servicepb.ExecutionState_EXECUTION_STATE_PENDING_DISPATCH,
	store.ExecutionDispatching:     action_servicepb.ExecutionState_EXECUTION_STATE_DISPATCHING,
	store.ExecutionRunning:         action_servicepb.ExecutionState_EXECUTION_STATE_RUNNING,
	store.ExecutionSucceeded:       action_servicepb.ExecutionState_EXECUTION_STATE_SUCCEEDED,
	store.ExecutionFailed:          action_servicepb.ExecutionState_EXECUTION_STATE_FAILED,
	store.ExecutionCancelled:       action_servicepb.ExecutionState_EXECUTION_STATE_CANCELLED,
	store.ExecutionUnknown:         action_servicepb.ExecutionState_EXECUTION_STATE_EXECUTION_UNKNOWN,
}

var verdicts = map[store.Verdict]action_servicepb.Verdict{
	store.VerdictAllow: action_servicepb.Verdict_VERDICT_ALLOW,
	store.VerdictDeny:  action_servicepb.Verdict_VERDICT_DENY,
}

var cancellationOutcomes = map[store.CancellationOutcome]action_servicepb.CancellationOutcome{
	store.OutcomeCancelled:        action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED,
	store.OutcomeAlreadyCancelled: action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_CANCELLED,
	store.OutcomeAlreadyFinished:  action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_FINISHED,
	store.OutcomeTooLate:          action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_TOO_LATE,
}

var policyKinds = map[store.PolicyKind]action_servicepb.PolicyKind{
	store.PolicyExactActions:               action_servicepb.PolicyKind_POLICY_KIND_EXACT_ACTIONS,
	store.PolicyArgumentSchema:             action_servicepb.PolicyKind_POLICY_KIND_ARGUMENT_SCHEMA,
	store.PolicyGitHubRepository:           action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY,
	store.PolicyGitHubPublicRepository:     action_servicepb.PolicyKind_POLICY_KIND_GITHUB_PUBLIC_REPOSITORY,
	store.PolicyHomeAssistantEntityControl: action_servicepb.PolicyKind_POLICY_KIND_HOME_ASSISTANT_ENTITY_CONTROL,
}

var policySources = map[string]action_servicepb.PolicySource{
	store.PolicySourceRules: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
}

func reversed[Stored, View comparable](views map[Stored]View) map[View]Stored {
	stored := make(map[View]Stored, len(views))
	for value, view := range views {
		stored[view] = value
	}
	return stored
}

var storedVerdicts = reversed(verdicts)

// lookup is a stored value's message form; a value the API has no form for is a stored request it cannot show.
func lookup[Stored comparable, View any](views map[Stored]View, field string, value Stored) (View, error) {
	view, known := views[value]
	if !known {
		return view, fmt.Errorf("%w: %s %v", errStoredRequest, field, value)
	}
	return view, nil
}

func timestamp(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamppb.New(*t)
}

func operator(principal store.OperatorPrincipal) *action_servicepb.Operator {
	return &action_servicepb.Operator{Issuer: principal.Issuer, Subject: principal.Subject}
}

// jsonView reads a stored JSON document into its message form.
func jsonView(field string, document json.RawMessage, view interface{ UnmarshalJSON([]byte) error }) error {
	if err := view.UnmarshalJSON(document); err != nil {
		return fmt.Errorf("%w: %s: %w", errStoredRequest, field, err)
	}
	return nil
}

func requestView(request store.ActionRequest) (*action_servicepb.ActionRequest, error) {
	state, ok := request.State.Proto()
	if !ok {
		return nil, fmt.Errorf("%w: state %q", errStoredRequest, request.State)
	}
	arguments := &structpb.Struct{}
	if err := jsonView("arguments", request.Arguments, arguments); err != nil {
		return nil, err
	}
	decision, err := decisionView(request.Decision)
	if err != nil {
		return nil, err
	}
	execution, err := executionView(request.Execution)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.ActionRequest{
		Id: request.ID.String(), IdempotencyKey: request.IdempotencyKey,
		Action:    &action_servicepb.ActionIdentity{Group: request.Action.Group, Name: request.Action.Name},
		Arguments: arguments, Title: request.Title, Description: request.Description,
		Caller: request.Caller.Proto(), State: state, Version: request.Version,
		CreatedAt: timestamppb.New(request.CreatedAt), UpdatedAt: timestamppb.New(request.UpdatedAt),
		Decision: decision, Execution: execution,
	}, nil
}

func requestViews(requests []store.ActionRequest) ([]*action_servicepb.ActionRequest, error) {
	views := make([]*action_servicepb.ActionRequest, len(requests))
	for i, request := range requests {
		var err error
		if views[i], err = requestView(request); err != nil {
			return nil, err
		}
	}
	return views, nil
}

func decisionView(decision *store.Decision) (*action_servicepb.Decision, error) {
	if decision == nil {
		return nil, nil
	}
	verdict, err := lookup(verdicts, "verdict", decision.Verdict)
	if err != nil {
		return nil, err
	}
	view := &action_servicepb.Decision{
		Id: decision.ID.String(), Verdict: verdict, Provider: decision.Provider, DecisionNote: decision.DecisionNote,
		ReasonCode: decision.ReasonCode, ReasonDescription: decision.ReasonDescription,
		IdempotencyKey: decision.IdempotencyKey, DecidedAt: timestamppb.New(decision.DecidedAt),
	}
	if decision.Operator != nil {
		view.Operator = operator(*decision.Operator)
	}
	if decision.PolicyEvidence != nil {
		if view.PolicyEvidence, err = evidenceView(*decision.PolicyEvidence); err != nil {
			return nil, err
		}
	}
	return view, nil
}

func evidenceView(evidence store.PolicyEvidence) (*action_servicepb.PolicyEvidence, error) {
	view := &action_servicepb.PolicyEvidence{}
	for _, binding := range evidence.Bindings {
		view.Bindings = append(view.Bindings, &action_servicepb.BindingEvidence{
			Namespace: binding.Namespace, Name: binding.Name, ResourceVersion: binding.ResourceVersion,
		})
	}
	for _, set := range evidence.PolicySets {
		view.PolicySets = append(view.PolicySets, &action_servicepb.PolicySetEvidence{
			Namespace: set.Namespace, Name: set.Name, Generation: set.Generation,
		})
	}
	matched := evidence.Matched
	source, err := lookup(policySources, "policy source", matched.Source)
	if err != nil {
		return nil, err
	}
	kind, err := lookup(policyKinds, "policy kind", matched.Type)
	if err != nil {
		return nil, err
	}
	if matched.Index > math.MaxInt32 {
		return nil, fmt.Errorf("%w: policy index %d", errStoredRequest, matched.Index)
	}
	view.Matched = &action_servicepb.MatchedPolicy{
		Namespace: matched.Namespace, PolicySet: matched.PolicySet, Source: source, Index: int32(matched.Index), Type: kind,
	}
	if repository := matched.Repository; repository != nil {
		view.Matched.Repository = &action_servicepb.MatchedRepository{
			Owner: repository.Owner, Repository: repository.Repository, ConfirmedPublic: repository.ConfirmedPublic,
		}
	}
	return view, nil
}

func executionView(execution *store.Execution) (*action_servicepb.Execution, error) {
	if execution == nil {
		return nil, nil
	}
	state, err := lookup(executionStates, "execution state", execution.State)
	if err != nil {
		return nil, err
	}
	view := &action_servicepb.Execution{
		Id: execution.ID.String(), State: state, CreatedAt: timestamppb.New(execution.CreatedAt),
		StartedAt: timestamp(execution.StartedAt), CompletedAt: timestamp(execution.CompletedAt),
		ReconciledAt: timestamp(execution.ReconciledAt),
	}
	if execution.Result != nil {
		view.Result = &structpb.Value{}
		if err := jsonView("result", execution.Result, view.Result); err != nil {
			return nil, err
		}
	}
	if execution.Error != nil {
		view.Error = &structpb.Struct{}
		if err := jsonView("error", execution.Error, view.Error); err != nil {
			return nil, err
		}
	}
	return view, nil
}

func eventView(event store.Event) (*action_servicepb.ActionEvent, error) {
	state, ok := event.State.Proto()
	if !ok {
		return nil, fmt.Errorf("%w: event state %q", errStoredRequest, event.State)
	}
	if event.Sequence > math.MaxInt32 {
		return nil, fmt.Errorf("%w: event sequence %d", errStoredRequest, event.Sequence)
	}
	view := &action_servicepb.ActionEvent{Sequence: int32(event.Sequence), State: state, At: timestamppb.New(event.At)}
	actor, err := action.ActorToProto(event.Actor)
	if err != nil {
		return nil, fmt.Errorf("%w: event actor: %w", errStoredRequest, err)
	}
	view.Actor = actor
	return view, nil
}

// requestedStates reads a request's state filter; `ACTION_STATE_UNSPECIFIED` and a number the API does not
// define are malformed.
func requestedStates(states []action_servicepb.ActionState) ([]action.State, error) {
	stored := make([]action.State, len(states))
	for i, state := range states {
		var known bool
		if stored[i], known = action.StateFromProto(state); !known {
			return nil, invalidRequest("state %v is not an action state", state)
		}
	}
	return stored, nil
}
