package mcpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strings"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// tools serve Action calls and static guide reads. Action calls reach core with the verified caller's context;
// guide reads use the edge's embedded documentation and create no Action.
type tools struct {
	core              pb.ActionServiceClient
	maxWaitSeconds    float64
	directWaitSeconds float64
}

type actionIdentity struct {
	Group string `json:"group"`
	Name  string `json:"name"`
}

// waitOptions is a call's wait object; the input schema supplies its defaults and bounds its values.
type waitOptions struct {
	WaitSeconds float64 `json:"wait_seconds"`
	WaitUntil   string  `json:"wait_until"`
}

type listActionsInput struct {
	Group         *string         `json:"group"`
	After         *actionIdentity `json:"after"`
	Limit         int             `json:"limit"`
	IncludeFields []string        `json:"include_fields"`
}

type getActionInput struct {
	Group         string   `json:"group"`
	Name          string   `json:"name"`
	IncludeFields []string `json:"include_fields"`
}

type getActionPolicyInput struct {
	Target        policyTarget `json:"target"`
	IncludeFields []string     `json:"include_fields"`
}

// policyTarget is "self", or a named ServiceAccount.
type policyTarget struct {
	account *sandboxes.ServiceAccount
}

func (t *policyTarget) UnmarshalJSON(data []byte) error {
	if bytes.Equal(data, []byte(`"self"`)) {
		return nil
	}
	var named struct {
		ServiceAccount struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		} `json:"service_account"`
	}
	if err := json.Unmarshal(data, &named); err != nil {
		return err
	}
	t.account = &sandboxes.ServiceAccount{Namespace: named.ServiceAccount.Namespace, Name: named.ServiceAccount.Name}
	return nil
}

type requestActionInput struct {
	Request struct {
		IdempotencyKey string         `json:"idempotency_key"`
		Action         actionIdentity `json:"action"`
		Arguments      map[string]any `json:"arguments"`
		Title          string         `json:"title"`
		Description    *string        `json:"description"`
	} `json:"request"`
	Wait          waitOptions `json:"wait"`
	IncludeFields []string    `json:"include_fields"`
	RespondWith   string      `json:"respond_with"`
}

// namedRequest names one of the caller's requests by exactly one of its id and its idempotency key.
type namedRequest struct {
	RequestID      *string     `json:"request_id"`
	IdempotencyKey *string     `json:"idempotency_key"`
	Wait           waitOptions `json:"wait"`
}

type getActionRequestInput struct {
	namedRequest
	IncludeFields []string `json:"include_fields"`
}

type cancelActionRequestInput struct {
	RequestID     string   `json:"request_id"`
	IncludeFields []string `json:"include_fields"`
}

type listActionRequestEventsInput struct {
	RequestID     string `json:"request_id"`
	AfterSequence int64  `json:"after_sequence"`
	Limit         int    `json:"limit"`
}

func (t *tools) listActions(ctx context.Context, input listActionsInput) (*mcp.CallToolResult, error) {
	response, err := t.core.ListActionGroups(ctx, &pb.ListActionGroupsRequest{})
	if err != nil {
		return nil, err
	}
	groups := slices.SortedFunc(slices.Values(response.GetActionGroups()), func(a, b *pb.ActionGroup) int {
		return strings.Compare(a.GetKey(), b.GetKey())
	})
	if input.Group != nil && !slices.ContainsFunc(groups, func(group *pb.ActionGroup) bool {
		return group.GetKey() == *input.Group
	}) {
		return nil, unknownGroup
	}
	if input.After != nil && input.Group != nil && input.After.Group != *input.Group {
		return nil, afterElsewhere
	}
	view := actionPage{Actions: []actionSummary{}}
scan:
	for _, group := range groups {
		if input.Group != nil && group.GetKey() != *input.Group {
			continue
		}
		actions := slices.SortedFunc(slices.Values(group.GetActions()), func(a, b *pb.Action) int {
			return strings.Compare(a.GetName(), b.GetName())
		})
		for _, action := range actions {
			if input.After != nil && !after(group.GetKey(), action.GetName(), *input.After) {
				continue
			}
			if len(view.Actions) == input.Limit {
				last := view.Actions[len(view.Actions)-1]
				view.NextAfter = &actionIdentity{Group: last.Group, Name: last.Name}
				break scan
			}
			view.Actions = append(view.Actions, summarize(group.GetKey(), group.GetAvailable(), action, input.IncludeFields))
		}
	}
	return structuredResult(view)
}

type actionPage struct {
	Actions   []actionSummary `json:"actions"`
	NextAfter *actionIdentity `json:"next_after,omitempty"`
}

// after reports whether group/name comes after the cursor in (group, name) order.
func after(group, name string, cursor actionIdentity) bool {
	if order := strings.Compare(group, cursor.Group); order != 0 {
		return order > 0
	}
	return name > cursor.Name
}

type actionSummary struct {
	Group       string                  `json:"group"`
	Name        string                  `json:"name"`
	Available   bool                    `json:"available"`
	InputSchema shown[*structpb.Struct] `json:"input_schema,omitzero"`
	Description shown[string]           `json:"description,omitzero"`
}

func summarize(group string, available bool, action *pb.Action, fields []string) actionSummary {
	view := actionSummary{Group: group, Name: action.GetName(), Available: available}
	if slices.Contains(fields, "input_schema") {
		view.InputSchema = show(object(action.GetInputSchema()))
	}
	if slices.Contains(fields, "description") {
		view.Description = show(action.GetDescription())
	}
	return view
}

func (t *tools) getAction(ctx context.Context, input getActionInput) (*mcp.CallToolResult, error) {
	response, err := t.core.GetAction(ctx, &pb.GetActionRequest{GroupKey: input.Group, ActionKey: input.Name})
	if err != nil {
		return nil, actionError(err, input.Group, input.Name)
	}
	// The core answers UNAVAILABLE for a group that is not available.
	return structuredResult(summarize(input.Group, true, response.GetAction(), input.IncludeFields))
}

func (t *tools) getActionPolicy(ctx context.Context, input getActionPolicyInput) (*mcp.CallToolResult, error) {
	response, err := t.core.GetActionPolicy(ctx, &pb.GetActionPolicyRequest{Subject: input.Target.account})
	if err != nil {
		return nil, err
	}
	d := &viewDecoder{}
	view := d.policyView(response.GetActionPolicy(), input.IncludeFields)
	if d.err != nil {
		return nil, d.err
	}
	return structuredResult(view)
}

func (t *tools) requestAction(ctx context.Context, input requestActionInput) (*mcp.CallToolResult, error) {
	if input.Wait.WaitSeconds > t.maxWaitSeconds {
		return nil, waitTooLong(t.maxWaitSeconds)
	}
	if input.RespondWith == "result" && !sameFields(input.IncludeFields, defaultReceiptFields) {
		return nil, fieldsNeedReceipt
	}
	arguments, err := structpb.NewStruct(input.Request.Arguments)
	if err != nil {
		return nil, fmt.Errorf("arguments: %w", err)
	}
	submitted, err := t.core.SubmitActionRequest(ctx, &pb.SubmitActionRequestRequest{
		IdempotencyKey: input.Request.IdempotencyKey,
		Action:         &pb.ActionIdentity{Group: input.Request.Action.Group, Name: input.Request.Action.Name},
		Arguments:      arguments,
		Title:          input.Request.Title,
		Description:    input.Request.Description,
	})
	if err != nil {
		return nil, submitError(err)
	}
	request := submitted.GetActionRequest()
	if input.Wait.WaitSeconds != 0 {
		if request, err = t.read(ctx, request.GetId(), input.Wait); err != nil {
			return nil, err
		}
	}
	switch input.RespondWith {
	case "result":
		executor, found, err := t.executor(ctx, request.GetAction().GetGroup())
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, fmt.Errorf("group %q of request %s is not configured", request.GetAction().GetGroup(), request.GetId())
		}
		return actionResult(request, executor, t.maxWaitSeconds)
	case "receipt":
		return receiptResult(request, input.IncludeFields)
	default:
		return nil, fmt.Errorf("respond_with %q", input.RespondWith)
	}
}

func sameFields(a, b []string) bool {
	for _, field := range a {
		if !slices.Contains(b, field) {
			return false
		}
	}
	for _, field := range b {
		if !slices.Contains(a, field) {
			return false
		}
	}
	return true
}

func receiptResult(request *pb.ActionRequest, fields []string) (*mcp.CallToolResult, error) {
	d := &viewDecoder{}
	view := d.receiptView(request, fields)
	if d.err != nil {
		return nil, d.err
	}
	return structuredResult(view)
}

func (t *tools) getActionRequest(ctx context.Context, input getActionRequestInput) (*mcp.CallToolResult, error) {
	request, err := t.namedReceipt(ctx, input.namedRequest)
	if err != nil {
		return nil, err
	}
	return receiptResult(request, input.IncludeFields)
}

func (t *tools) getActionResult(ctx context.Context, input namedRequest) (*mcp.CallToolResult, error) {
	request, err := t.namedReceipt(ctx, input)
	if err != nil {
		return nil, err
	}
	executor, found, err := t.executor(ctx, request.GetAction().GetGroup())
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, groupGone
	}
	return actionResult(request, executor, t.maxWaitSeconds)
}

// namedReceipt reads the request a caller names, waiting as it asks.
func (t *tools) namedReceipt(ctx context.Context, input namedRequest) (*pb.ActionRequest, error) {
	if input.Wait.WaitSeconds > t.maxWaitSeconds {
		return nil, waitTooLong(t.maxWaitSeconds)
	}
	if (input.RequestID == nil) == (input.IdempotencyKey == nil) {
		return nil, nameOneRequest
	}
	var requestID string
	if input.RequestID != nil {
		id, err := parseRequestID(*input.RequestID)
		if err != nil {
			return nil, err
		}
		requestID = id
	} else {
		listed, err := t.core.ListActionRequests(ctx, &pb.ListActionRequestsRequest{IdempotencyKey: input.IdempotencyKey})
		if err != nil {
			return nil, requestError(err)
		}
		switch requests := listed.GetActionRequests(); len(requests) {
		case 0:
			return nil, requestNotFound
		case 1:
			requestID = requests[0].GetId()
		default:
			return nil, fmt.Errorf("core answered %d requests for one idempotency key", len(requests))
		}
	}
	return t.read(ctx, requestID, input.Wait)
}

func parseRequestID(text string) (string, error) {
	id, err := uuid.Parse(text)
	if err != nil {
		return "", toolError(fmt.Sprintf("request_id %q is not a UUID: %v", text, err))
	}
	return id.String(), nil
}

// executor is the executor kind of a configured group, and whether the group is configured.
func (t *tools) executor(ctx context.Context, group string) (pb.ExecutorKind, bool, error) {
	response, err := t.core.ListActionGroups(ctx, &pb.ListActionGroupsRequest{})
	if err != nil {
		return 0, false, err
	}
	for _, configured := range response.GetActionGroups() {
		if configured.GetKey() == group {
			return configured.GetExecutorKind(), true, nil
		}
	}
	return 0, false, nil
}

type cancellation struct {
	Outcome string  `json:"outcome"`
	Request receipt `json:"request"`
}

func (t *tools) cancelActionRequest(ctx context.Context, input cancelActionRequestInput) (*mcp.CallToolResult, error) {
	requestID, err := parseRequestID(input.RequestID)
	if err != nil {
		return nil, err
	}
	response, err := t.core.CancelActionRequest(ctx, &pb.CancelActionRequestRequest{RequestId: requestID})
	if err != nil {
		return nil, requestError(err)
	}
	d := &viewDecoder{}
	view := cancellation{
		Outcome: enum(d, response.GetOutcome(), "CANCELLATION_OUTCOME_"),
		Request: d.receiptView(response.GetActionRequest(), input.IncludeFields),
	}
	if d.err != nil {
		return nil, d.err
	}
	return structuredResult(view)
}

type eventPage struct {
	Events            []event `json:"events"`
	NextAfterSequence *int32  `json:"next_after_sequence,omitempty"`
}

func (t *tools) listActionRequestEvents(ctx context.Context, input listActionRequestEventsInput) (*mcp.CallToolResult, error) {
	requestID, err := parseRequestID(input.RequestID)
	if err != nil {
		return nil, err
	}
	// One more than the page shows whether another follows. No event's sequence exceeds the int32 range, so a
	// larger cursor reads the same empty page as its largest value.
	extra := int32(input.Limit + 1)
	response, err := t.core.ListActionRequestEvents(ctx, &pb.ListActionRequestEventsRequest{
		RequestId:     requestID,
		AfterSequence: int32(min(input.AfterSequence, math.MaxInt32)),
		Limit:         &extra,
	})
	if err != nil {
		return nil, requestError(err)
	}
	events := response.GetEvents()
	d := &viewDecoder{}
	view := eventPage{Events: []event{}}
	for _, from := range events[:min(len(events), input.Limit)] {
		view.Events = append(view.Events, d.eventView(from))
	}
	if len(events) > input.Limit {
		next := events[input.Limit-1].GetSequence()
		view.NextAfterSequence = &next
	}
	if d.err != nil {
		return nil, d.err
	}
	return structuredResult(view)
}
