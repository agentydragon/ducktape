package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// orderedIDs are the requests' IDs in the order the service answered them.
func orderedIDs(requests []*action_servicepb.ActionRequest) []string {
	ids := make([]string, len(requests))
	for i, request := range requests {
		ids[i] = request.GetId()
	}
	return ids
}

func structOf(t *testing.T, fields map[string]any) *structpb.Struct {
	t.Helper()
	view, err := structpb.NewStruct(fields)
	if err != nil {
		t.Fatal(err)
	}
	return view
}

func TestAReceiptCarriesItsPolicyDecisionAndItsExecutionsOutcome(t *testing.T) {
	s := serve(t)
	description, reason := "test description", "test reason description"
	evidence := &store.PolicyEvidence{
		Bindings:   []store.BindingEvidence{{Namespace: testNamespace, Name: "test-binding", ResourceVersion: "7"}},
		PolicySets: []store.PolicySetEvidence{{Namespace: testNamespace, Name: "test-set", Generation: 4}},
		Matched: store.MatchedPolicy{
			Namespace: testNamespace, PolicySet: "test-set", Source: store.PolicySourceRules, Index: 2,
			Type:       store.PolicyGitHubRepository,
			Repository: &store.MatchedRepository{Owner: "test-owner", Repository: "test-repository", ConfirmedPublic: true},
		},
	}
	submitted, err := s.store.Submit(t.Context(), caller, store.ActionRequestInput{
		IdempotencyKey: "test-voted", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{"test": ["argument", 1]}`), Title: "test title", Description: &description,
	}, store.Admission{RequestID: uuid.New(), Vote: &store.ProviderVote{
		Provider: "test-provider", Verdict: store.VerdictAllow, ReasonCode: "test_reason", ReasonDescription: &reason,
		Evidence: evidence,
	}})
	if err != nil {
		t.Fatal(err)
	}
	claim, err := s.store.ClaimExecution(t.Context(), submitted.ID, "test-executor", time.Minute, func(store.ActionIdentity) bool { return true })
	if err != nil || claim == nil {
		t.Fatalf("claim: %v, %v", claim, err)
	}
	if _, err := s.store.MarkRunning(t.Context(), submitted.ID); err != nil {
		t.Fatal(err)
	}
	if err := s.store.FinishExecution(t.Context(), submitted.ID, claim.Lease, store.ExecutionOutcome{
		State: store.ExecutionFailed, Result: json.RawMessage(`["test-partial", 1]`),
		Error: json.RawMessage(`{"kind": "test_failure", "message": "test message"}`),
	}); err != nil {
		t.Fatal(err)
	}
	// IDs and times are the ones the store assigned.
	stored, err := s.store.Get(t.Context(), submitted.ID, caller)
	if err != nil {
		t.Fatal(err)
	}

	got, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: submitted.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	result, err := structpb.NewList([]any{"test-partial", 1})
	if err != nil {
		t.Fatal(err)
	}
	want := &action_servicepb.ActionRequest{
		Id: submitted.ID.String(), IdempotencyKey: "test-voted",
		Action:    &action_servicepb.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: structOf(t, map[string]any{"test": []any{"argument", 1}}), Title: "test title", Description: &description,
		Caller:    &sandboxes.ServiceAccount{Namespace: testNamespace, Name: "test-caller"},
		CallerUid: proto.String("test-uid-test-caller"),
		State:     action_servicepb.ActionState_ACTION_STATE_FAILED, Version: stored.Version,
		CreatedAt: timestamppb.New(stored.CreatedAt), UpdatedAt: timestamppb.New(stored.UpdatedAt),
		Decision: &action_servicepb.Decision{
			Id: stored.Decision.ID.String(), Verdict: action_servicepb.Verdict_VERDICT_ALLOW, Provider: "test-provider",
			ReasonCode: proto.String("test_reason"), ReasonDescription: &reason,
			PolicyEvidence: &action_servicepb.PolicyEvidence{
				Bindings:   []*action_servicepb.BindingEvidence{{Namespace: testNamespace, Name: "test-binding", ResourceVersion: "7"}},
				PolicySets: []*action_servicepb.PolicySetEvidence{{Namespace: testNamespace, Name: "test-set", Generation: 4}},
				Matched: &action_servicepb.MatchedPolicy{
					Namespace: testNamespace, PolicySet: "test-set", Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
					Index: 2, Type: action_servicepb.PolicyKind_POLICY_KIND_GITHUB_REPOSITORY,
					Repository: &action_servicepb.MatchedRepository{Owner: "test-owner", Repository: "test-repository", ConfirmedPublic: true},
				},
			},
			IdempotencyKey: stored.Decision.IdempotencyKey, DecidedAt: timestamppb.New(stored.Decision.DecidedAt),
		},
		Execution: &action_servicepb.Execution{
			Id: stored.Execution.ID.String(), State: action_servicepb.ExecutionState_EXECUTION_STATE_FAILED,
			Result:    structpb.NewListValue(result),
			Error:     structOf(t, map[string]any{"kind": "test_failure", "message": "test message"}),
			CreatedAt: timestamppb.New(stored.Execution.CreatedAt), StartedAt: timestamppb.New(*stored.Execution.StartedAt),
			CompletedAt: timestamppb.New(*stored.Execution.CompletedAt),
		},
	}
	if !proto.Equal(got.GetActionRequest(), want) {
		t.Errorf("receipt\n%v\nwant\n%v", got.GetActionRequest(), want)
	}
}

func TestAnOperatorsDecisionAndACallersCancellationNameWhoActed(t *testing.T) {
	s := serve(t)
	decided := s.decide(t, s.submit(t, caller, "test-decided"), store.VerdictDeny)
	got, err := s.client.GetActionRequest(as(t, readerToken), &action_servicepb.GetActionRequestRequest{RequestId: decided.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	decision := got.GetActionRequest().GetDecision()
	if got.GetActionRequest().GetState() != action_servicepb.ActionState_ACTION_STATE_DENIED ||
		decision.GetVerdict() != action_servicepb.Verdict_VERDICT_DENY ||
		!proto.Equal(decision.GetOperator(), &action_servicepb.Operator{Issuer: s.issuer.url, Subject: operatorSubject}) {
		t.Errorf("operator's decision: %v", got.GetActionRequest())
	}

	cancelled := s.submit(t, caller, "test-cancelled")
	if _, err := s.store.Cancel(t.Context(), cancelled.ID, caller); err != nil {
		t.Fatal(err)
	}
	stored, err := s.store.Events(t.Context(), cancelled.ID, caller, store.EventsFilter{})
	if err != nil || len(stored) != 2 {
		t.Fatalf("stored events %v, %v", stored, err)
	}
	all := []*action_servicepb.ActionEvent{
		{Sequence: 1, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, At: timestamppb.New(stored[0].At)},
		{
			Sequence: 2, State: action_servicepb.ActionState_ACTION_STATE_CANCELLED, At: timestamppb.New(stored[1].At),
			Actor: &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{
				Caller: &sandboxes.ServiceAccount{Namespace: testNamespace, Name: "test-caller"},
			}, CallerUid: proto.String("test-uid-test-caller")},
		},
	}
	for name, tc := range map[string]struct {
		request *action_servicepb.ListActionRequestEventsRequest
		want    []*action_servicepb.ActionEvent
	}{
		"every event":        {&action_servicepb.ListActionRequestEventsRequest{}, all},
		"after the first":    {&action_servicepb.ListActionRequestEventsRequest{AfterSequence: 1}, all[1:]},
		"the first only":     {&action_servicepb.ListActionRequestEventsRequest{Limit: proto.Int32(1)}, all[:1]},
		"after the last one": {&action_servicepb.ListActionRequestEventsRequest{AfterSequence: 2}, nil},
	} {
		tc.request.RequestId = cancelled.ID.String()
		got, err := s.client.ListActionRequestEvents(as(t, s.operatorToken), tc.request)
		if err != nil || !slices.EqualFunc(got.GetEvents(), tc.want, func(a, b *action_servicepb.ActionEvent) bool { return proto.Equal(a, b) }) {
			t.Errorf("%s: %v, %v; want %v", name, got.GetEvents(), err, tc.want)
		}
	}
}

func TestListingSelectsByStateOrKeyAndPagesNewestFirst(t *testing.T) {
	s := serve(t)
	first := s.submit(t, caller, "test-first")
	second := s.submit(t, caller, "test-second")
	third := s.decide(t, s.submit(t, caller, "test-third"), store.VerdictDeny)
	pending := action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING
	denied := action_servicepb.ActionState_ACTION_STATE_DENIED
	for name, tc := range map[string]struct {
		request *action_servicepb.ListActionRequestsRequest
		want    []store.ActionRequest
	}{
		"every request":            {&action_servicepb.ListActionRequestsRequest{}, []store.ActionRequest{third, second, first}},
		"pending":                  {&action_servicepb.ListActionRequestsRequest{State: []action_servicepb.ActionState{pending}}, []store.ActionRequest{second, first}},
		"denied":                   {&action_servicepb.ListActionRequestsRequest{State: []action_servicepb.ActionState{denied}}, []store.ActionRequest{third}},
		"by key":                   {&action_servicepb.ListActionRequestsRequest{IdempotencyKey: proto.String("test-second")}, []store.ActionRequest{second}},
		"the first page":           {&action_servicepb.ListActionRequestsRequest{Limit: proto.Int32(2)}, []store.ActionRequest{third, second}},
		"the page after the first": {&action_servicepb.ListActionRequestsRequest{Limit: proto.Int32(2), AfterId: proto.String(second.ID.String())}, []store.ActionRequest{first}},
	} {
		got, err := s.client.ListActionRequests(as(t, callerToken), tc.request)
		want := make([]string, len(tc.want))
		for i, request := range tc.want {
			want[i] = request.ID.String()
		}
		if err != nil || !slices.Equal(orderedIDs(got.GetActionRequests()), want) {
			t.Errorf("%s: %v, %v; want %v", name, orderedIDs(got.GetActionRequests()), err, want)
		}
	}

	others := s.submit(t, otherCaller, "test-others")
	for name, after := range map[string]string{"an unknown request": uuid.NewString(), "another caller's request": others.ID.String()} {
		_, err := s.client.ListActionRequests(as(t, callerToken), &action_servicepb.ListActionRequestsRequest{AfterId: proto.String(after)})
		wantCode(t, "after "+name, err, codes.NotFound)
	}
}

func TestHistoryPagesTheDecidedRequestsAndCanLeaveOutThoseAPolicyAllowed(t *testing.T) {
	s := serve(t)
	auto, err := s.store.Submit(t.Context(), caller, store.ActionRequestInput{
		IdempotencyKey: "test-auto-allowed", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{}`), Title: "test title",
	}, store.Admission{RequestID: uuid.New(), Vote: &store.ProviderVote{Provider: "test-provider", Verdict: store.VerdictAllow, ReasonCode: "test_reason"}})
	if err != nil {
		t.Fatal(err)
	}
	denied := s.decide(t, s.submit(t, caller, "test-denied"), store.VerdictDeny)
	allowed := s.decide(t, s.submit(t, caller, "test-allowed"), store.VerdictAllow)
	s.submit(t, caller, "test-pending")
	history := func(request *action_servicepb.ListActionRequestHistoryRequest) *action_servicepb.ListActionRequestHistoryResponse {
		t.Helper()
		page, err := s.client.ListActionRequestHistory(as(t, callerToken), request)
		if err != nil {
			t.Fatal(err)
		}
		return page
	}

	first := history(&action_servicepb.ListActionRequestHistoryRequest{Limit: proto.Int32(2)})
	if !slices.Equal(orderedIDs(first.GetActionRequests()), []string{allowed.ID.String(), denied.ID.String()}) || first.NextCursor == nil {
		t.Errorf("first page: %v", first)
	}
	last := history(&action_servicepb.ListActionRequestHistoryRequest{Limit: proto.Int32(2), Cursor: first.NextCursor})
	if !slices.Equal(orderedIDs(last.GetActionRequests()), []string{auto.ID.String()}) || last.NextCursor != nil {
		t.Errorf("last page: %v", last)
	}
	decidedByOperator := history(&action_servicepb.ListActionRequestHistoryRequest{IncludeAutoAllowed: proto.Bool(false)})
	if !slices.Equal(orderedIDs(decidedByOperator.GetActionRequests()), []string{allowed.ID.String(), denied.ID.String()}) {
		t.Errorf("without the requests a policy allowed: %v", decidedByOperator)
	}
}

func TestAMalformedRequestIsAnInvalidArgument(t *testing.T) {
	s := serve(t)
	ctx := as(t, callerToken)
	request := s.submit(t, caller, "test-request").ID.String()
	list := func(request *action_servicepb.ListActionRequestsRequest) error {
		_, err := s.client.ListActionRequests(ctx, request)
		return err
	}
	history := func(request *action_servicepb.ListActionRequestHistoryRequest) error {
		_, err := s.client.ListActionRequestHistory(ctx, request)
		return err
	}
	events := func(request *action_servicepb.ListActionRequestEventsRequest) error {
		_, err := s.client.ListActionRequestEvents(ctx, request)
		return err
	}
	_, getErr := s.client.GetActionRequest(ctx, &action_servicepb.GetActionRequestRequest{RequestId: "test-not-a-uuid"})
	for name, err := range map[string]error{
		"a request ID that is not a UUID":           getErr,
		"an unspecified state":                      list(&action_servicepb.ListActionRequestsRequest{State: []action_servicepb.ActionState{action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED}}),
		"a state the API does not define":           list(&action_servicepb.ListActionRequestsRequest{State: []action_servicepb.ActionState{99}}),
		"a page of no requests":                     list(&action_servicepb.ListActionRequestsRequest{Limit: proto.Int32(0)}),
		"a page of 129 requests":                    list(&action_servicepb.ListActionRequestsRequest{Limit: proto.Int32(129)}),
		"an after_id that is not a UUID":            list(&action_servicepb.ListActionRequestsRequest{AfterId: proto.String("test-not-a-uuid")}),
		"a history page of no requests":             history(&action_servicepb.ListActionRequestHistoryRequest{Limit: proto.Int32(0)}),
		"a history page of 101 requests":            history(&action_servicepb.ListActionRequestHistoryRequest{Limit: proto.Int32(101)}),
		"a cursor the service never gave":           history(&action_servicepb.ListActionRequestHistoryRequest{Cursor: proto.String("test-not-a-cursor")}),
		"events of a request ID that is not a UUID": events(&action_servicepb.ListActionRequestEventsRequest{RequestId: "test-not-a-uuid"}),
		"a negative after_sequence":                 events(&action_servicepb.ListActionRequestEventsRequest{RequestId: request, AfterSequence: -1}),
		"a page of no events":                       events(&action_servicepb.ListActionRequestEventsRequest{RequestId: request, Limit: proto.Int32(0)}),
		"a page of 129 events":                      events(&action_servicepb.ListActionRequestEventsRequest{RequestId: request, Limit: proto.Int32(129)}),
	} {
		wantCode(t, name, err, codes.InvalidArgument)
	}
	// The largest pages are accepted.
	for name, err := range map[string]error{
		"a page of 128 requests":         list(&action_servicepb.ListActionRequestsRequest{Limit: proto.Int32(128)}),
		"a history page of 100 requests": history(&action_servicepb.ListActionRequestHistoryRequest{Limit: proto.Int32(100)}),
		"a page of 128 events":           events(&action_servicepb.ListActionRequestEventsRequest{RequestId: request, Limit: proto.Int32(128)}),
	} {
		if err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestAnUnknownRequestIsNotFound(t *testing.T) {
	s := serve(t)
	unknown := uuid.NewString()
	_, err := s.client.GetActionRequest(as(t, s.operatorToken), &action_servicepb.GetActionRequestRequest{RequestId: unknown})
	wantCode(t, "the receipt", err, codes.NotFound)
	_, err = s.client.ListActionRequestEvents(as(t, s.operatorToken), &action_servicepb.ListActionRequestEventsRequest{RequestId: unknown})
	wantCode(t, "the events", err, codes.NotFound)
}

func TestAStoredRequestTheAPICannotShowIsAnInternalError(t *testing.T) {
	s := serve(t)
	unshowable := s.submit(t, caller, "test-unshowable")
	shown := s.submit(t, caller, "test-shown")
	if _, err := s.db.ExecContext(t.Context(), `UPDATE action_request SET arguments = '[]'::jsonb WHERE id = $1`, unshowable.ID); err != nil {
		t.Fatal(err)
	}
	_, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: unshowable.ID.String()})
	wantCode(t, "the unshowable request", err, codes.Internal)
	if _, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: shown.ID.String()}); err != nil {
		t.Errorf("another request: %v", err)
	}
}

func TestTheHTTPFacadeAnswersAsTheRPCs(t *testing.T) {
	s := serve(t)
	request := s.submit(t, caller, "test-http").ID.String()
	code, body := s.get(t, "/v1/action-requests/"+request, callerToken)
	answered := &action_servicepb.GetActionRequestResponse{}
	if code != http.StatusOK || protojson.Unmarshal(body, answered) != nil {
		t.Fatalf("receipt: %d %s", code, body)
	}
	rpc, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: request})
	if err != nil || !proto.Equal(answered, rpc) {
		t.Errorf("the HTTP receipt %v differs from the RPC's %v, %v", answered, rpc, err)
	}
	code, body = s.get(t, "/v1/action-requests?state=ACTION_STATE_DECISION_PENDING", s.operatorToken)
	listed := &action_servicepb.ListActionRequestsResponse{}
	if code != http.StatusOK || protojson.Unmarshal(body, listed) != nil || !slices.Equal(orderedIDs(listed.GetActionRequests()), []string{request}) {
		t.Errorf("pending requests: %d %s", code, body)
	}
	for name, tc := range map[string]struct {
		path, token string
		code        int
	}{
		"history":                         {"/v1/action-requests:history", readerToken, http.StatusOK},
		"events":                          {"/v1/action-requests/" + request + "/events?after_sequence=0", callerToken, http.StatusOK},
		"no bearer":                       {"/v1/action-requests/" + request, "", http.StatusUnauthorized},
		"another caller's receipt":        {"/v1/action-requests/" + request, otherToken, http.StatusNotFound},
		"a request ID that is not a UUID": {"/v1/action-requests/test-not-a-uuid", callerToken, http.StatusBadRequest},
	} {
		if code, body := s.get(t, tc.path, tc.token); code != tc.code {
			t.Errorf("%s: %d %s, want %d", name, code, body, tc.code)
		}
	}
}

func (s *served) cancel(t *testing.T, token string, request store.ActionRequest) (*action_servicepb.CancelActionRequestResponse, error) {
	t.Helper()
	return s.client.CancelActionRequest(as(t, token), &action_servicepb.CancelActionRequestRequest{RequestId: request.ID.String()})
}

// claimable is whether an executor could still claim the request's dispatch: a request cancelled while pending
// has no Execution, and one cancelled once allowed has a cancelled one.
func (s *served) claimable(t *testing.T, request store.ActionRequest) bool {
	t.Helper()
	claim, err := s.store.ClaimExecution(t.Context(), request.ID, "test-executor", time.Minute, func(store.ActionIdentity) bool { return true })
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("claim: %v", err)
	}
	return claim != nil
}

func TestTheOwnerCancelsARequestUntilItsDispatchIsClaimed(t *testing.T) {
	s := serve(t)
	pending := s.submit(t, caller, "test-pending")
	allowed := s.decide(t, s.submit(t, caller, "test-allowed"), store.VerdictAllow)
	denied := s.decide(t, s.submit(t, caller, "test-denied"), store.VerdictDeny)
	claimed := s.decide(t, s.submit(t, caller, "test-claimed"), store.VerdictAllow)
	s.claim(t, claimed)
	for name, tc := range map[string]struct {
		request store.ActionRequest
		outcome action_servicepb.CancellationOutcome
		state   action_servicepb.ActionState
	}{
		"a pending request":  {pending, action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED, action_servicepb.ActionState_ACTION_STATE_CANCELLED},
		"an allowed request": {allowed, action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED, action_servicepb.ActionState_ACTION_STATE_CANCELLED},
		"a denied request":   {denied, action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_FINISHED, action_servicepb.ActionState_ACTION_STATE_DENIED},
		"a claimed request":  {claimed, action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_TOO_LATE, action_servicepb.ActionState_ACTION_STATE_DISPATCHING},
	} {
		got, err := s.cancel(t, callerToken, tc.request)
		if err != nil || got.GetOutcome() != tc.outcome || got.GetActionRequest().GetState() != tc.state {
			t.Errorf("%s: %v in %v, %v; want %v in %v", name, got.GetOutcome(), got.GetActionRequest().GetState(), err, tc.outcome, tc.state)
		}
	}
	again, err := s.cancel(t, callerToken, pending)
	if err != nil || again.GetOutcome() != action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_CANCELLED {
		t.Errorf("cancelling again: %v, %v", again, err)
	}
	for _, request := range []store.ActionRequest{pending, allowed} {
		if s.claimable(t, request) {
			t.Errorf("the cancelled request %s was claimed", request.IdempotencyKey)
		}
	}
}

func TestOnlyTheOwnerMayCancel(t *testing.T) {
	s := serve(t)
	request := s.submit(t, caller, "test-pending")
	for name, tc := range map[string]struct {
		token string
		code  codes.Code
	}{
		"another caller":   {otherToken, codes.NotFound},
		"a service reader": {readerToken, codes.NotFound},
		"an operator":      {s.operatorToken, codes.PermissionDenied},
	} {
		_, err := s.cancel(t, tc.token, request)
		wantCode(t, name, err, tc.code)
	}
	if got, err := s.client.GetActionRequest(as(t, callerToken), &action_servicepb.GetActionRequestRequest{RequestId: request.ID.String()}); err != nil ||
		got.GetActionRequest().GetVersion() != request.Version {
		t.Errorf("the refused cancellations changed the request: %v, %v", got, err)
	}
}

func TestACancellationThatCannotBeAnsweredFails(t *testing.T) {
	s := serve(t)
	_, err := s.client.CancelActionRequest(as(t, callerToken), &action_servicepb.CancelActionRequestRequest{RequestId: "test-not-a-uuid"})
	wantCode(t, "a request ID that is not a UUID", err, codes.InvalidArgument)
	_, err = s.cancel(t, callerToken, store.ActionRequest{ID: uuid.New()})
	wantCode(t, "an unknown request", err, codes.NotFound)
}
