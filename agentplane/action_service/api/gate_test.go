package api

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
	unbound, err := kube.UnboundToken(t.Context(), callerAccount, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	own := s.decide(t, s.submit(t, caller, "test-own"), store.VerdictAllow)
	others := s.decide(t, s.submit(t, otherCaller, "test-others"), store.VerdictDeny)
	// The service reader carries no caller label: a reader account needs none.
	for name, tc := range map[string]struct {
		token string
		want  []string
	}{
		"the owner":        {unbound, idsOf(own)},
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
	_, err = s.client.GetActionRequest(as(t, otherToken), &action_servicepb.GetActionRequestRequest{RequestId: own.ID.String()})
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
	var refusals []string
	for name, ctx := range map[string]context.Context{
		"no bearer":                                presenting(t),
		"a token for another audience":             as(t, otherAudience),
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
	s := serveWith(t, kubeapiserver.UnreachableReviews{Err: errors.New("test API server unreachable")}, &catalog.Catalog{})
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

// The standalone MCP edge's ConnectionService uses this shared Gate: operators pass to the service, while workload
// tokens are refused before Kubernetes TokenReview.
func TestOnlyAnOperatorPassesTheGateToTheConnectionService(t *testing.T) {
	reviews := &kubeapiserver.CountedReviews{TokenReviews: kube.TokenReviews()}
	s := serveWith(t, reviews, &catalog.Catalog{})
	for _, token := range []string{callerToken, readerToken} {
		_, err := s.connections.ListConnections(as(t, token), &connection_servicepb.ListConnectionsRequest{})
		wantCode(t, "a workload's call", err, codes.Unauthenticated)
	}
	if calls := reviews.Calls.Load(); calls != 0 {
		t.Errorf("%d workload tokens reviewed", calls)
	}
	_, err := s.connections.ListConnections(as(t, s.operatorToken), &connection_servicepb.ListConnectionsRequest{})
	wantCode(t, "an operator's call", err, codes.Unimplemented)
}

func connectionContext(t *testing.T, ctx context.Context, actorToken string, envelope *delegationpb.Delegation) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	md := metadata.Pairs("authorization", auth.DelegatedScheme+" "+actorToken)
	md.Set(auth.DelegationMetadataKey, string(encoded))
	return metadata.NewOutgoingContext(ctx, md)
}

func TestConnectionDelegationUsesAnExactIdentityAndRechecksItOnActionStreams(t *testing.T) {
	s := serve(t)
	namespace := policyNamespace(s.policy)
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "connection-subject"},
		Spec:       policyapi.IdentitySpec{Description: "external MCP test subject"},
	}
	if err := policyObjects.Create(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	actor := auth.Account{Namespace: testNamespace, Name: "test-mcp-edge"}
	if err := setCallerLabel(t.Context(), actor, true); err != nil {
		t.Fatal(err)
	}
	actorToken, err := kube.WorkloadToken(t.Context(), actor, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	workloads, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	identityUIDs, err := auth.KubernetesIdentityUIDs(policyObjects, s.policy)
	if err != nil {
		t.Fatal(err)
	}
	methods := []string{
		action_servicepb.ActionService_SubmitActionRequest_FullMethodName,
		action_servicepb.ActionService_ListActionRequests_FullMethodName,
		action_servicepb.ActionService_WatchActionPolicy_FullMethodName,
	}
	rules := make(auth.Methods, len(methods))
	for _, method := range methods {
		rules[method] = auth.ReviewedServiceAccountOrIdentity(workloads)
	}
	s.gate.connectionDelegation, err = rules.WithDelegation(methods, []auth.Delegator{{
		Account: actor, IdentityCallers: true, ConnectionProvenance: true,
	}}, identityUIDs)
	if err != nil {
		t.Fatal(err)
	}
	s.gate.callers = s.index
	eventually(t, "MCP edge Action caller label", func() bool { return s.index.Admits(actor) })
	connectionID, grantID := uuid.NewString(), uuid.NewString()
	connectionEnvelope := func(subject *identitypb.Identity) *delegationpb.Delegation {
		return &delegationpb.Delegation{
			EffectiveCaller: &delegationpb.Delegation_Identity{Identity: subject},
			Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{
				ConnectionId: connectionID, GrantId: grantID,
			}},
		}
	}
	identityRef := &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}
	delegated := func(subject *identitypb.Identity) context.Context {
		return connectionContext(t, t.Context(), actorToken, connectionEnvelope(subject))
	}
	request := envelope(t, "connection-identity-submit", "test-group", "test-action", map[string]any{"target": "test-target"})
	if _, err := s.client.SubmitActionRequest(delegated(identityRef), request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("an Identity with no PolicyBinding was admitted through the labeled actor: %v", err)
	}

	// The method allowlist is exact; the shared auth contract rejects legacy ServiceAccount callers.
	_, err = s.client.GetActionPolicy(delegated(identityRef), &action_servicepb.GetActionPolicyRequest{})
	wantCode(t, "an Action method without Connection delegation", err, codes.Unauthenticated)

	set := namespace.policySet(t, "connection-subject-set", exactActions("other-group", "not-this-action"))
	binding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "connection-subject-binding"},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}}, PolicySets: []string{set.Name},
		},
	}
	if err := policyObjects.Create(t.Context(), binding); err != nil {
		t.Fatal(err)
	}
	eventually(t, "Identity PolicyBinding", func() bool {
		return s.index.AdmitsIdentity(auth.Identity{Name: identity.Name, UID: string(identity.UID)})
	})
	submitted, err := s.client.SubmitActionRequest(delegated(identityRef), request)
	if err != nil {
		t.Fatal(err)
	}
	if submitted.GetActionRequest().GetCallerIdentity().GetUid() != string(identity.UID) || submitted.GetActionRequest().GetCaller() != nil {
		t.Fatalf("Action request was attributed to an SA instead of the effective Identity: %v", submitted.GetActionRequest())
	}
	listed, err := s.client.ListActionRequests(delegated(identityRef), &action_servicepb.ListActionRequestsRequest{})
	if err != nil || len(listed.GetActionRequests()) != 1 {
		t.Fatalf("Identity could not read its own request: response=%v err=%v", listed, err)
	}
	actorList, err := s.client.ListActionRequests(as(t, actorToken), &action_servicepb.ListActionRequestsRequest{})
	if err != nil || len(actorList.GetActionRequests()) != 0 {
		t.Fatalf("delegation fell back to the actor's ServiceAccount: response=%v err=%v", actorList, err)
	}

	streamContext, cancel := context.WithTimeout(delegated(identityRef), 12*time.Second)
	defer cancel()
	stream, err := s.client.WatchActionPolicy(streamContext, &action_servicepb.GetActionPolicyRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("initial Identity policy frame: %v", err)
	}
	if err := policyObjects.Delete(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	successor := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: identity.Name},
		Spec:       policyapi.IdentitySpec{Description: "replacement Identity"},
	}
	if err := policyObjects.Create(t.Context(), successor); err != nil {
		t.Fatal(err)
	}
	if string(successor.UID) == string(identity.UID) {
		t.Fatal("test API server reused an Identity UID")
	}
	_, err = stream.Recv()
	wantCode(t, "an open Action stream after same-name Identity replacement", err, codes.PermissionDenied)
}
