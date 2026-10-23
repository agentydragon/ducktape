package api

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/google/uuid"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

// providerTimeout bounds the decision provider at admission: a GitHub lookup that has not answered
// by then confirms nothing, so the request waits for an operator.
const providerTimeout = 5 * time.Second

// Submissions is what SubmitActionRequest admits a request against, beside the catalog, which holds the Actions a
// request may name with their input schemas, and the policy index.
type Submissions struct {
	// Executors says which of the catalog's Actions this service runs: an Action no Executor runs is refused.
	Executors worker.Executors
	// Provider decides from the caller's PolicyBindings as the policy index resolves them at admission.
	Provider *policies.Provider
}

func (s *Service) SubmitActionRequest(ctx context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
	var caller store.CallerPrincipal
	switch principal := admitted(ctx).principal.(type) {
	case store.CallerPrincipal:
		caller = principal
	case store.OperatorPrincipal:
		return nil, status.Error(codes.PermissionDenied, "an operator cannot submit a request")
	default:
		// A service reader acts as no caller: it is refused as an account without the caller label is.
		return nil, unauthenticated
	}
	input, arguments, err := submission(request)
	if err != nil {
		return nil, err
	}
	definition, err := s.runnable(input.Action)
	if err != nil {
		return nil, err
	}
	switch err := definition.CheckArguments(arguments); {
	case errors.Is(err, catalog.ErrInvalidArguments):
		return nil, status.Error(codes.InvalidArgument, err.Error())
	case err != nil:
		slog.ErrorContext(ctx, "an Action's input schema cannot be used",
			"group", input.Action.Group, "action", input.Action.Name, "error", err)
		return nil, status.Error(codes.Internal, "the Action's input schema cannot be used")
	}
	admission := store.Admission{RequestID: uuid.New()}
	switch decision := s.decide(ctx, caller, input.Action, arguments).(type) {
	case policies.Allow:
		admission.Vote = &decision.Vote
	case policies.NoOpinion:
		if request.GetDecidedOnly() {
			// Nothing is stored, so no operator is asked and the key stays unused.
			return nil, status.Error(codes.PermissionDenied, decision.ReasonDescription)
		}
	}
	submitted, err := s.store.Submit(ctx, caller, input, admission)
	if err != nil {
		return nil, err
	}
	view, err := requestView(submitted)
	if err != nil {
		return nil, err
	}
	return &action_servicepb.SubmitActionRequestResponse{ActionRequest: view}, nil
}

// submission reads a submission's envelope, and its arguments as jsonschema.UnmarshalJSON decodes them, which is how
// the input schema and the policies read them.
func submission(request *action_servicepb.SubmitActionRequestRequest) (store.ActionRequestInput, map[string]any, error) {
	if request.GetArguments() == nil {
		return store.ActionRequestInput{}, nil, invalidRequest("arguments are required: {} for an Action without arguments")
	}
	document, err := protojson.Marshal(request.GetArguments())
	if err != nil {
		return store.ActionRequestInput{}, nil, invalidRequest("arguments are not JSON: %v", err)
	}
	input := store.ActionRequestInput{
		IdempotencyKey: request.GetIdempotencyKey(),
		Action:         store.ActionIdentity{Group: request.GetAction().GetGroup(), Name: request.GetAction().GetName()},
		Arguments:      document, Title: request.GetTitle(), Description: request.Description,
	}
	if err := input.Validate(); err != nil {
		return store.ActionRequestInput{}, nil, err
	}
	arguments, err := jsonschema.UnmarshalJSON(bytes.NewReader(document))
	if err != nil {
		return store.ActionRequestInput{}, nil, invalidRequest("arguments are not JSON: %v", err)
	}
	// Validate held them to a JSON object.
	return input, arguments.(map[string]any), nil
}

// runnable is the definition of an Action this service runs. One the catalog does not offer, or of a group no
// Executor runs, it cannot run; one of a group that is unavailable it may run once the group is back.
func (s *Service) runnable(action store.ActionIdentity) (catalog.ActionDefinition, error) {
	_, definition, err := s.catalog.Resolve(action.Group, action.Name)
	switch {
	case errors.Is(err, catalog.ErrUnavailable):
		return definition, status.Error(codes.Unavailable, catalog.ErrUnavailable.Error())
	case err != nil:
		return definition, status.Errorf(codes.InvalidArgument, "unsupported group/action %s/%s", action.Group, action.Name)
	}
	if executor, _ := s.submissions.Executors.Route(action); executor == nil {
		return definition, status.Errorf(codes.InvalidArgument, "unsupported group/action %s/%s", action.Group, action.Name)
	}
	return definition, nil
}

// decide asks the decision provider about the request, against the caller's bindings as the policy index holds them
// now. Nothing is decided while the index is unavailable.
func (s *Service) decide(ctx context.Context, caller store.CallerPrincipal, action store.ActionIdentity, arguments map[string]any) policies.Decision {
	ctx, cancel := context.WithTimeout(ctx, providerTimeout)
	defer cancel()
	bindings := s.policy.ResolveBindings(policyapi.ServiceAccountRef{
		Namespace: caller.Account.Namespace, Name: caller.Account.Name,
	})
	return s.submissions.Provider.Decide(ctx, bindings, action, arguments)
}
