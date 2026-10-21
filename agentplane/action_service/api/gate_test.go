package api

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
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
	var refusals []string
	for name, ctx := range map[string]context.Context{
		"no bearer":            presenting(t),
		"two bearers":          presenting(t, "Bearer "+callerToken, "Bearer "+callerToken),
		"another scheme":       presenting(t, "Basic "+callerToken),
		"an unknown token":     as(t, "test-token-unknown"),
		"an unlabeled account": as(t, unlabeledToken),
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

func TestAWorkloadIsRefusedOnceItsAccountLosesTheLabelOrItsTokenIsRevoked(t *testing.T) {
	s := serve(t)
	read := func(token string) error {
		_, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{})
		return err
	}
	for _, token := range []string{callerToken, otherToken} {
		if err := read(token); err != nil {
			t.Fatal(err)
		}
	}
	s.labels.remove(workload(callerToken))
	wantCode(t, "an account that lost the label", read(callerToken), codes.Unauthenticated)
	s.reviews.revoke(otherToken)
	wantCode(t, "a revoked token", read(otherToken), codes.Unauthenticated)
}

func TestAWorkloadIsUnavailableWhileTheTokenReviewGoesUnanswered(t *testing.T) {
	s := serve(t)
	s.reviews.setUnavailable()
	_, err := s.client.ListActionRequests(as(t, callerToken), &action_servicepb.ListActionRequestsRequest{})
	wantCode(t, "a workload", err, codes.Unavailable)
	// An operator's token is verified against the issuer's keys alone.
	if _, err := s.client.ListActionRequests(as(t, s.operatorToken), &action_servicepb.ListActionRequestsRequest{}); err != nil {
		t.Errorf("an operator: %v", err)
	}
}

func TestADrainingServiceRefusesEveryCall(t *testing.T) {
	s := serve(t)
	s.gate.Drain()
	for _, token := range []string{callerToken, readerToken, s.operatorToken} {
		_, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{})
		wantCode(t, "a call while draining", err, codes.Unavailable)
	}
}

func TestAnRPCNotServedYetIsUnimplementedOnceAdmitted(t *testing.T) {
	s := serve(t)
	submission := &action_servicepb.SubmitActionRequestRequest{IdempotencyKey: "test-" + uuid.NewString()}
	_, err := s.client.SubmitActionRequest(as(t, callerToken), submission)
	wantCode(t, "an admitted caller", err, codes.Unimplemented)
	_, err = s.client.SubmitActionRequest(as(t, "test-token-unknown"), submission)
	wantCode(t, "an unknown token", err, codes.Unauthenticated)
}
