package api

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func requestIDs(requests []*action_servicepb.ActionRequest) []string {
	ids := make([]string, len(requests))
	for i, request := range requests {
		ids[i] = request.GetId()
	}
	return slices.Sorted(slices.Values(ids))
}

func idsOf(requests ...store.ActionRequest) []string {
	ids := make([]string, len(requests))
	for i, request := range requests {
		ids[i] = request.ID.String()
	}
	return slices.Sorted(slices.Values(ids))
}

func TestEachPrincipalReadsTheRequestsItMayAndEachSeesTheSameReceipt(t *testing.T) {
	s := serve(t)
	own := s.decide(t, s.submit(t, caller, "test-own"), store.VerdictAllow)
	others := s.decide(t, s.submit(t, otherCaller, "test-others"), store.VerdictDeny)
	// The service reader carries no caller label: a reader account needs none.
	for name, tc := range map[string]struct {
		token string
		want  []string
	}{
		"the owner":        {callerToken, idsOf(own)},
		"another caller":   {otherToken, idsOf(others)},
		"a service reader": {readerToken, idsOf(own, others)},
		"an operator":      {s.operatorToken, idsOf(own, others)},
	} {
		listed, err := s.client.ListActionRequests(as(t, tc.token), &action_servicepb.ListActionRequestsRequest{})
		if err != nil || !slices.Equal(requestIDs(listed.GetActionRequests()), tc.want) {
			t.Errorf("%s lists %v, %v; want %v", name, requestIDs(listed.GetActionRequests()), err, tc.want)
		}
		history, err := s.client.ListActionRequestHistory(as(t, tc.token), &action_servicepb.ListActionRequestHistoryRequest{})
		if err != nil || !slices.Equal(requestIDs(history.GetActionRequests()), tc.want) {
			t.Errorf("%s's history is %v, %v; want %v", name, requestIDs(history.GetActionRequests()), err, tc.want)
		}
	}

	var receipts []*action_servicepb.ActionRequest
	for _, token := range []string{callerToken, readerToken, s.operatorToken} {
		got, err := s.client.GetActionRequest(as(t, token), &action_servicepb.GetActionRequestRequest{RequestId: own.ID.String()})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := s.client.ListActionRequestEvents(as(t, token), &action_servicepb.ListActionRequestEventsRequest{RequestId: own.ID.String()}); err != nil {
			t.Error(err)
		}
		receipts = append(receipts, got.GetActionRequest())
	}
	for _, receipt := range receipts[1:] {
		if !proto.Equal(receipt, receipts[0]) {
			t.Errorf("readers see different receipts:\n%v\n%v", receipts[0], receipt)
		}
	}
	_, err := s.client.GetActionRequest(as(t, otherToken), &action_servicepb.GetActionRequestRequest{RequestId: own.ID.String()})
	wantCode(t, "another caller's receipt", err, codes.NotFound)
	_, err = s.client.ListActionRequestEvents(as(t, otherToken), &action_servicepb.ListActionRequestEventsRequest{RequestId: own.ID.String()})
	wantCode(t, "another caller's events", err, codes.NotFound)
}

// presenting is a call context that sends each value as an `authorization` header.
func presenting(t *testing.T, values ...string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	for _, value := range values {
		ctx = metadata.AppendToOutgoingContext(ctx, "authorization", value)
	}
	return ctx
}

func TestABearerTheServiceDoesNotAcceptIsRefusedWithoutSayingWhy(t *testing.T) {
	s := serve(t)
	otherAudience, err := kube.WorkloadToken(t.Context(), callerAccount, "test-other-audience")
	if err != nil {
		t.Fatal(err)
	}
	unbound, err := kube.UnboundToken(t.Context(), callerAccount, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	var refusals []string
	for name, ctx := range map[string]context.Context{
		"no bearer":                                presenting(t),
		"a token for another audience":             as(t, otherAudience),
		"a token bound to no Pod":                  as(t, unbound),
		"two bearers":                              presenting(t, "Bearer "+callerToken, "Bearer "+callerToken),
		"another scheme":                           presenting(t, "Basic "+callerToken),
		"an unknown token":                         as(t, "test-token-unknown"),
		"an unlabeled account":                     as(t, unlabeledToken),
		"an account outside the caller namespaces": as(t, outsiderToken),
		"an expired operator token":                as(t, s.issuer.token(t, map[string]any{"exp": time.Now().Add(-time.Hour).Unix()})),
		"another issuer's token":                   as(t, s.issuer.token(t, map[string]any{"iss": "https://test-other-issuer.example/"})),
		"an operator token for another party":      as(t, s.issuer.token(t, map[string]any{"azp": "test-other-party"})),
	} {
		_, err := s.client.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
		wantCode(t, name, err, codes.Unauthenticated)
		refusals = append(refusals, status.Convert(err).Message())
	}
	if refusals = slices.Compact(slices.Sorted(slices.Values(refusals))); len(refusals) != 1 {
		t.Errorf("the refusals say why: %q", refusals)
	}
}

func TestAWorkloadIsRefusedOnceItsAccountLosesTheLabelOrItsPodIsGone(t *testing.T) {
	s := serve(t)
	// Accounts of this test's own, so that changing them leaves the other tests' alone.
	delabeled := auth.Account{Namespace: testNamespace, Name: "test-delabeled-caller"}
	revoked := auth.Account{Namespace: testNamespace, Name: "test-revoked-caller"}
	for _, account := range []auth.Account{delabeled, revoked} {
		if err := setCallerLabel(t.Context(), account, true); err != nil {
			t.Fatal(err)
		}
	}
	delabeledToken, err := kube.WorkloadToken(t.Context(), delabeled, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	pod, err := kube.Pod(t.Context(), revoked)
	if err != nil {
		t.Fatal(err)
	}
	revokedToken, err := kube.PodToken(t.Context(), pod, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	read := func(token string) error {
		_, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{})
		return err
	}
	for _, token := range []string{delabeledToken, revokedToken} {
		if err := read(token); err != nil {
			t.Fatal(err)
		}
	}
	if err := setCallerLabel(t.Context(), delabeled, false); err != nil {
		t.Fatal(err)
	}
	wantCode(t, "an account that lost the label", read(delabeledToken), codes.Unauthenticated)
	if err := kube.Revoke(t.Context(), pod, revokedToken, testAudience); err != nil {
		t.Fatal(err)
	}
	wantCode(t, "the token of a deleted Pod", read(revokedToken), codes.Unauthenticated)
}

func TestAWorkloadIsUnavailableWhileTheAPIServerCannotBeReached(t *testing.T) {
	s := serveWith(t, kubeapiserver.UnreachableReviews{Err: errors.New("test API server unreachable")})
	_, err := s.client.ListActionRequests(as(t, callerToken), &action_servicepb.ListActionRequestsRequest{})
	wantCode(t, "a workload", err, codes.Unavailable)
	// An operator's token is verified against the issuer's keys alone.
	if _, err := s.client.ListActionRequests(as(t, s.operatorToken), &action_servicepb.ListActionRequestsRequest{}); err != nil {
		t.Errorf("an operator: %v", err)
	}
}

func TestADrainingServiceRefusesEveryCall(t *testing.T) {
	s := serve(t)
	request := s.submit(t, caller, "test-pending")
	s.gate.Drain()
	for _, token := range []string{callerToken, readerToken, s.operatorToken} {
		_, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{})
		wantCode(t, "a read while draining", err, codes.Unavailable)
	}
	_, err := s.cancel(t, callerToken, request)
	wantCode(t, "a cancellation while draining", err, codes.Unavailable)
	_, err = s.decideAs(t, s.operatorToken, decision(request, allow))
	wantCode(t, "a Decision while draining", err, codes.Unavailable)
}

func TestAnRPCNotServedYetIsUnimplementedOnceAdmitted(t *testing.T) {
	s := serve(t)
	_, err := s.operators.ListMcpServers(as(t, s.operatorToken), &operator_action_servicepb.ListMcpServersRequest{})
	wantCode(t, "an admitted operator", err, codes.Unimplemented)
	_, err = s.operators.ListMcpServers(as(t, "test-token-unknown"), &operator_action_servicepb.ListMcpServersRequest{})
	wantCode(t, "an unknown token", err, codes.Unauthenticated)
}
