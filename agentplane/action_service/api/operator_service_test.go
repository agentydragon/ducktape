package api

import (
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// decision is an operator's Decision on the request at the version it was submitted at, under a key of its own.
func decision(request store.ActionRequest, verdict action_servicepb.Verdict) *operator_action_servicepb.DecideActionRequestRequest {
	return &operator_action_servicepb.DecideActionRequestRequest{
		RequestId: request.ID.String(), Verdict: verdict, ExpectedVersion: request.Version,
		IdempotencyKey: "test-decision-" + request.IdempotencyKey,
	}
}

func (s *served) decideAs(t *testing.T, token string, request *operator_action_servicepb.DecideActionRequestRequest) (*action_servicepb.ActionRequest, error) {
	t.Helper()
	decided, err := s.operators.DecideActionRequest(as(t, token), request)
	return decided.GetActionRequest(), err
}

const (
	allow = action_servicepb.Verdict_VERDICT_ALLOW
	deny  = action_servicepb.Verdict_VERDICT_DENY
)

func TestAnOperatorsDecisionIsRecordedAndAnAllowQueuesTheDispatch(t *testing.T) {
	s := serve(t)
	allowed := s.submit(t, caller, "test-allowed")
	allowing := decision(allowed, allow)
	allowing.DecisionNote = proto.String("test note for the caller")
	got, err := s.decideAs(t, s.operatorToken, allowing)
	if err != nil {
		t.Fatal(err)
	}
	if got.GetState() != action_servicepb.ActionState_ACTION_STATE_ALLOWED ||
		got.GetDecision().GetVerdict() != allow || got.GetDecision().GetDecisionNote() != "test note for the caller" ||
		!proto.Equal(got.GetDecision().GetOperator(), &action_servicepb.Operator{Issuer: s.issuer.url, Subject: operatorSubject}) ||
		got.GetExecution().GetState() != action_servicepb.ExecutionState_EXECUTION_STATE_PENDING_DISPATCH {
		t.Errorf("the allowed request: %v", got)
	}
	if !s.claimable(t, allowed) {
		t.Error("the allowed request awaits no dispatch")
	}
	// An identical retry answers with the request as it now stands, and dispatches nothing more.
	retried, err := s.decideAs(t, s.operatorToken, allowing)
	if err != nil || retried.GetState() != action_servicepb.ActionState_ACTION_STATE_DISPATCHING {
		t.Errorf("the retry: %v, %v", retried, err)
	}

	denied := s.submit(t, caller, "test-denied")
	got, err = s.decideAs(t, s.operatorToken, decision(denied, deny))
	if err != nil || got.GetState() != action_servicepb.ActionState_ACTION_STATE_DENIED || got.GetExecution() != nil {
		t.Errorf("the denied request: %v, %v", got, err)
	}
	if s.claimable(t, denied) {
		t.Error("the denied request was claimed")
	}
}

func TestADecisionThatCannotBeRecordedFails(t *testing.T) {
	s := serve(t)
	decided := s.submit(t, caller, "test-decided")
	if _, err := s.decideAs(t, s.operatorToken, decision(decided, allow)); err != nil {
		t.Fatal(err)
	}
	pending := s.submit(t, caller, "test-pending")
	expired := s.issuer.token(t, map[string]any{"exp": time.Now().Add(-time.Hour).Unix()})
	for name, tc := range map[string]struct {
		change func(*operator_action_servicepb.DecideActionRequestRequest)
		token  string
		code   codes.Code
	}{
		"a request already decided": {func(r *operator_action_servicepb.DecideActionRequestRequest) {
			r.RequestId, r.ExpectedVersion = decided.ID.String(), decided.Version+1
		}, s.operatorToken, codes.FailedPrecondition},
		"a version the operator did not review": {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.ExpectedVersion++ }, s.operatorToken, codes.Aborted},
		"a key used on another request": {func(r *operator_action_servicepb.DecideActionRequestRequest) {
			r.IdempotencyKey = "test-decision-test-decided"
		}, s.operatorToken, codes.AlreadyExists},
		"a key used for another verdict": {func(r *operator_action_servicepb.DecideActionRequestRequest) {
			r.RequestId, r.Verdict, r.IdempotencyKey = decided.ID.String(), deny, "test-decision-test-decided"
		}, s.operatorToken, codes.AlreadyExists},
		"an unknown request":   {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.RequestId = uuid.NewString() }, s.operatorToken, codes.NotFound},
		"a malformed request":  {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.RequestId = "test-not-a-uuid" }, s.operatorToken, codes.InvalidArgument},
		"no verdict":           {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.Verdict = 0 }, s.operatorToken, codes.InvalidArgument},
		"an undefined verdict": {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.Verdict = 99 }, s.operatorToken, codes.InvalidArgument},
		"no version":           {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.ExpectedVersion = 0 }, s.operatorToken, codes.InvalidArgument},
		"no key":               {func(r *operator_action_servicepb.DecideActionRequestRequest) { r.IdempotencyKey = "" }, s.operatorToken, codes.InvalidArgument},
		"a long key": {func(r *operator_action_servicepb.DecideActionRequestRequest) {
			r.IdempotencyKey = strings.Repeat("k", 201)
		}, s.operatorToken, codes.InvalidArgument},
		"a long note": {func(r *operator_action_servicepb.DecideActionRequestRequest) {
			r.DecisionNote = proto.String(strings.Repeat("n", 2001))
		}, s.operatorToken, codes.InvalidArgument},
		"no bearer":                 {func(*operator_action_servicepb.DecideActionRequestRequest) {}, "", codes.Unauthenticated},
		"an expired operator token": {func(*operator_action_servicepb.DecideActionRequestRequest) {}, expired, codes.Unauthenticated},
	} {
		request := decision(pending, allow)
		tc.change(request)
		_, err := s.decideAs(t, tc.token, request)
		wantCode(t, name, err, tc.code)
	}
	// The longest key and note are accepted.
	longest := decision(pending, deny)
	longest.IdempotencyKey, longest.DecisionNote = strings.Repeat("k", 200), proto.String(strings.Repeat("n", 2000))
	if _, err := s.decideAs(t, s.operatorToken, longest); err != nil {
		t.Errorf("the longest key and note: %v", err)
	}
}

func TestAWorkloadsBearerNeverReachesTheOperatorService(t *testing.T) {
	reviews := &kubeapiserver.CountedReviews{TokenReviews: kube.TokenReviews()}
	s := serveWith(t, reviews, &catalog.Catalog{})
	request := s.submit(t, caller, "test-pending")
	disconnection := &operator_action_servicepb.DisconnectMcpServerRequest{ServerId: testServer}
	for _, token := range []string{callerToken, readerToken} {
		_, err := s.decideAs(t, token, decision(request, allow))
		wantCode(t, "a workload's decision", err, codes.Unauthenticated)
		_, err = s.operators.DisconnectMcpServer(as(t, token), disconnection)
		wantCode(t, "a workload's disconnection", err, codes.Unauthenticated)
	}
	if calls := reviews.Calls.Load(); calls != 0 {
		t.Errorf("%d workload tokens reviewed", calls)
	}
	// An operator's reaches the service, which configures no MCP server.
	_, err := s.operators.DisconnectMcpServer(as(t, s.operatorToken), disconnection)
	wantCode(t, "an operator's disconnection", err, codes.NotFound)
}

func TestOfTwoOperatorsDecidingAtOnceOneDecidesAndTheOtherIsTold(t *testing.T) {
	s := serve(t)
	otherOperator := s.issuer.token(t, map[string]any{"sub": "test-other-operator"})
	for i := range 10 {
		request := s.submit(t, caller, fmt.Sprintf("test-contested-%d", i))
		var wg sync.WaitGroup
		start := make(chan struct{})
		errs := make([]error, 2)
		for j, token := range []string{s.operatorToken, otherOperator} {
			wg.Go(func() {
				<-start
				_, errs[j] = s.decideAs(t, token, decision(request, []action_servicepb.Verdict{allow, deny}[j]))
			})
		}
		close(start)
		wg.Wait()
		won := -1
		for j, err := range errs {
			switch code := status.Code(err); code {
			case codes.OK:
				if won >= 0 {
					t.Fatalf("both operators decided %s", request.ID)
				}
				won = j
			case codes.Aborted, codes.FailedPrecondition:
			default:
				t.Fatalf("operator %d: %v", j, err)
			}
		}
		if won < 0 {
			t.Fatalf("neither operator decided %s: %v", request.ID, errs)
		}
		got, err := s.client.GetActionRequest(as(t, readerToken), &action_servicepb.GetActionRequestRequest{RequestId: request.ID.String()})
		wantSubject := []string{operatorSubject, "test-other-operator"}[won]
		if err != nil || got.GetActionRequest().GetDecision().GetOperator().GetSubject() != wantSubject {
			t.Errorf("the request carries %v, %v; want the Decision of %s", got.GetActionRequest().GetDecision(), err, wantSubject)
		}
	}
}

func TestADecisionRacingTheOwnersCancellationNeverLeavesADispatch(t *testing.T) {
	s := serve(t)
	for i := range 10 {
		request := s.submit(t, caller, fmt.Sprintf("test-raced-%d", i))
		var wg sync.WaitGroup
		start := make(chan struct{})
		var decideErr, cancelErr error
		var cancelled *action_servicepb.CancelActionRequestResponse
		wg.Go(func() {
			<-start
			_, decideErr = s.decideAs(t, s.operatorToken, decision(request, allow))
		})
		wg.Go(func() {
			<-start
			cancelled, cancelErr = s.cancel(t, callerToken, request)
		})
		close(start)
		wg.Wait()
		// Whichever came first, the cancellation withdraws the request: from decision-pending, or from its pending
		// dispatch once allowed (no dispatch worker runs here to claim it in between), and a Decision after it finds
		// the request changed.
		if cancelErr != nil || cancelled.GetOutcome() != action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED ||
			cancelled.GetActionRequest().GetState() != action_servicepb.ActionState_ACTION_STATE_CANCELLED {
			t.Fatalf("the cancellation: %v, %v", cancelled, cancelErr)
		}
		if code := status.Code(decideErr); code != codes.OK && code != codes.Aborted {
			t.Fatalf("the Decision: %v", decideErr)
		}
		if s.claimable(t, request) {
			t.Fatalf("the cancelled request %s was claimed", request.ID)
		}
	}
}

func TestTheHTTPFacadeDecidesAndCancels(t *testing.T) {
	s := serve(t)
	post := func(path, token, body string) (int, []byte) {
		t.Helper()
		call, err := http.NewRequestWithContext(t.Context(), http.MethodPost, s.url+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		call.Header.Set("Authorization", "Bearer "+token)
		response, err := http.DefaultClient.Do(call)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		answer, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, answer
	}
	decided := s.submit(t, caller, "test-decided")
	path := "/v1/operator/action-requests/" + decided.ID.String() + "/decision"
	body := `{"verdict": "VERDICT_DENY", "expected_version": 1, "idempotency_key": "test-http-decision"}`
	if code, answer := post(path, callerToken, body); code != http.StatusUnauthorized {
		t.Errorf("a caller's decision: %d %s", code, answer)
	}
	code, answer := post(path, s.operatorToken, body)
	got := &operator_action_servicepb.DecideActionRequestResponse{}
	if code != http.StatusOK || protojson.Unmarshal(answer, got) != nil || got.GetActionRequest().GetState() != action_servicepb.ActionState_ACTION_STATE_DENIED {
		t.Errorf("the operator's decision: %d %s", code, answer)
	}

	cancelled := s.submit(t, caller, "test-cancelled")
	path = "/v1/action-requests/" + cancelled.ID.String() + ":cancel"
	if code, answer := post(path, s.operatorToken, ""); code != http.StatusForbidden {
		t.Errorf("an operator's cancellation: %d %s", code, answer)
	}
	code, answer = post(path, callerToken, "")
	withdrawn := &action_servicepb.CancelActionRequestResponse{}
	if code != http.StatusOK || protojson.Unmarshal(answer, withdrawn) != nil ||
		withdrawn.GetOutcome() != action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED {
		t.Errorf("the owner's cancellation: %d %s", code, answer)
	}
}
