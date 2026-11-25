package rpc

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
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
	own := s.decide(t, s.submit(t, s.caller, "test-own"), store.VerdictAllow)
	others := s.decide(t, s.submit(t, s.otherCaller, "test-others"), store.VerdictDeny)
	for name, tc := range map[string]struct {
		token string
		want  []string
	}{
		"the owner":        {callerCertificate, idsOf(own)},
		"another caller":   {otherCertificate, idsOf(others)},
		"a service reader": {readerCertificate, idsOf(own, others)},
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
	for _, token := range []string{callerCertificate, readerCertificate, s.operatorToken} {
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
	_, err := s.client.GetActionRequest(as(t, otherCertificate), &action_servicepb.GetActionRequestRequest{RequestId: own.ID.String()})
	wantCode(t, "another caller's receipt", err, codes.NotFound)
	_, err = s.client.ListActionRequestEvents(as(t, otherCertificate), &action_servicepb.ListActionRequestEventsRequest{RequestId: own.ID.String()})
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
		"valid Kubernetes bearer":             as(t, validKubernetesToken),
		"no bearer":                           presenting(t),
		"a token for another audience":        as(t, otherAudience),
		"two bearers":                         presenting(t, "Bearer "+validKubernetesToken, "Bearer "+validKubernetesToken),
		"another scheme":                      presenting(t, "Basic "+validKubernetesToken),
		"an unknown token":                    as(t, "test-token-unknown"),
		"an expired operator token":           as(t, s.issuer.Token(t, map[string]any{"exp": time.Now().Add(-time.Hour).Unix()})),
		"another issuer's token":              as(t, s.issuer.Token(t, map[string]any{"iss": "https://test-other-issuer.example/"})),
		"an operator token for another party": as(t, s.issuer.Token(t, map[string]any{"azp": "test-other-party"})),
	} {
		_, err := s.client.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
		wantCode(t, name, err, codes.Unauthenticated)
		refusals = append(refusals, status.Convert(err).Message())
	}
	if refusals = slices.Compact(slices.Sorted(slices.Values(refusals))); len(refusals) != 1 {
		t.Errorf("the refusals say why: %q", refusals)
	}
}

func TestWorkloadSVIDRequiresItsLiveCredentialAndIdentityBinding(t *testing.T) {
	s := serve(t)
	read := func(certificate string) error {
		_, err := s.client.ListActionRequests(as(t, certificate), &action_servicepb.ListActionRequestsRequest{})
		return err
	}
	for _, certificate := range []string{callerCertificate, otherCertificate} {
		if err := read(certificate); err != nil {
			t.Fatal(err)
		}
	}
	s.removeCallerBinding(t)
	wantCode(t, "Identity without a binding", read(callerCertificate), codes.Unauthenticated)
	s.revokeCertificate(t, otherCertificate)
	wantCode(t, "revoked workload credential", read(otherCertificate), codes.Unauthenticated)
	if _, err := s.client.ListActionRequests(as(t, s.operatorToken), &action_servicepb.ListActionRequestsRequest{}); err != nil {
		t.Fatal(err)
	}
}

func TestADrainingServiceRefusesEveryCall(t *testing.T) {
	s := serve(t)
	request := s.submit(t, s.caller, "test-pending")
	s.gate.Drain()
	for _, token := range []string{callerCertificate, readerCertificate, s.operatorToken} {
		_, err := s.client.ListActionRequests(as(t, token), &action_servicepb.ListActionRequestsRequest{})
		wantCode(t, "a read while draining", err, codes.Unavailable)
	}
	_, err := s.cancel(t, callerCertificate, request)
	wantCode(t, "a cancellation while draining", err, codes.Unavailable)
	_, err = s.decideAs(t, s.operatorToken, decision(request, allow))
	wantCode(t, "a Decision while draining", err, codes.Unavailable)
}

func connectionContext(t *testing.T, ctx context.Context, certificate string, envelope *delegationpb.Delegation) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	ctx = context.WithValue(ctx, certificateConnectionKey{}, certificate)
	return metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, string(encoded))
}

func TestConnectionDelegationUsesAnExactIdentityAndRechecksItOnActionStreams(t *testing.T) {
	s := serveWithGateOptions(t, func(options *grpcgate.Options, namespace policyNamespace) {
		identityUIDs, err := auth.KubernetesIdentityUIDs(policyObjects, string(namespace))
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
			rules[method], err = auth.Services(nil)
			if err != nil {
				t.Fatal(err)
			}
		}
		options.ConnectionDelegation, err = rules.WithServiceDelegation(methods, []auth.ServiceDelegator{{
			SPIFFEID: peertls.MCPEdgeURI, ConnectionProvenance: true,
		}}, identityUIDs)
		if err != nil {
			t.Fatal(err)
		}
		sessionMethods := []string{
			action_servicepb.ActionService_ListActionGroups_FullMethodName,
			action_servicepb.ActionService_SubmitActionRequest_FullMethodName,
			action_servicepb.ActionService_ListActionRequests_FullMethodName,
			action_servicepb.ActionService_GetActionRequest_FullMethodName,
			action_servicepb.ActionService_WatchActionRequest_FullMethodName,
		}
		sessionRules := make(auth.Methods, len(sessionMethods))
		for _, method := range sessionMethods {
			sessionRules[method], err = auth.Services(nil)
			if err != nil {
				t.Fatal(err)
			}
		}
		options.SessionDelegation, err = sessionRules.WithServiceDelegation(sessionMethods,
			[]auth.ServiceDelegator{{SPIFFEID: peertls.CentralSamplerURI}}, identityUIDs)
		if err != nil {
			t.Fatal(err)
		}
	})
	namespace := policyNamespace(s.policy)
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "connection-subject"},
		Spec:       policyapi.IdentitySpec{Description: "external MCP test subject"},
	}
	if err := policyObjects.Create(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
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
		return connectionContext(t, t.Context(), edgeCertificate, connectionEnvelope(subject))
	}
	request := envelope(t, "connection-identity-submit", "test-group", "test-action", map[string]any{"target": "test-target"})
	if _, err := s.client.SubmitActionRequest(delegated(identityRef), request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("an Identity with no PolicyBinding was admitted through the delegating actor: %v", err)
	}

	// The method allowlist is exact; the shared auth contract rejects legacy ServiceAccount callers.
	_, err := s.client.GetActionPolicy(delegated(identityRef), &action_servicepb.GetActionPolicyRequest{})
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
	eventually.Eventually(t, "Identity PolicyBinding", func() bool {
		return s.index.AdmitsIdentity(auth.Identity{Name: identity.Name, UID: string(identity.UID)})
	})
	submitted, err := s.client.SubmitActionRequest(delegated(identityRef), request)
	if err != nil {
		t.Fatal(err)
	}
	if submitted.GetActionRequest().GetCallerIdentity().GetUid() != string(identity.UID) || submitted.GetActionRequest().GetCaller() != nil {
		t.Fatalf("Action request was attributed to an SA instead of the effective Identity: %v", submitted.GetActionRequest())
	}
	receipt := submitted.GetActionRequest()
	if receipt.GetDelegatingActor().GetServiceSpiffeId() != peertls.MCPEdgeURI {
		t.Fatalf("receipt lost its service actor: %v", receipt)
	}
	wantProvenance := connectionEnvelope(identityRef).GetConnection()
	if !proto.Equal(receipt.GetConnectionProvenance(), wantProvenance) || receipt.GetSessionProvenance() != nil {
		t.Fatalf("receipt lost the admitting Connection provenance: %v", receipt)
	}
	listed, err := s.client.ListActionRequests(delegated(identityRef), &action_servicepb.ListActionRequestsRequest{})
	if err != nil || len(listed.GetActionRequests()) != 1 {
		t.Fatalf("Identity could not read its own request: response=%v err=%v", listed, err)
	}
	if !proto.Equal(listed.GetActionRequests()[0].GetDelegatingActor(), receipt.GetDelegatingActor()) || !proto.Equal(listed.GetActionRequests()[0].GetConnectionProvenance(), wantProvenance) {
		t.Fatalf("listing did not retain the admitting actor and provenance: %v", listed)
	}
	_, err = s.client.ListActionRequests(as(t, edgeCertificate), &action_servicepb.ListActionRequestsRequest{})
	wantCode(t, "delegator has no direct grant", err, codes.Unauthenticated)
	_, err = s.client.SubmitActionRequest(metadata.AppendToOutgoingContext(delegated(identityRef), "authorization", "Bearer "+validKubernetesToken), request)
	wantCode(t, "delegation plus Kubernetes bearer", err, codes.Unauthenticated)

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

	// A separately admitted sampler watch also rechecks its exact Session-profile Identity.
	sessionBinding := &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "session-subject-binding"},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: successor.Name}}, PolicySets: []string{set.Name},
		},
	}
	if err := policyObjects.Create(t.Context(), sessionBinding); err != nil {
		t.Fatal(err)
	}
	eventually.Eventually(t, "replacement Identity PolicyBinding", func() bool {
		return s.index.AdmitsIdentity(auth.Identity{Name: successor.Name, UID: string(successor.UID)})
	})
	samplerCA := transporttesting.NewCA(t)
	samplerLeaf := samplerCA.Issue(t, peertls.CentralSamplerURI)
	pair, err := tls.LoadX509KeyPair(samplerLeaf.CertFile, samplerLeaf.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	sessionEnvelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: successor.Name, Uid: string(successor.UID)}},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: uuid.NewString()}},
	}
	encoded, err := auth.MarshalDelegation(sessionEnvelope)
	if err != nil {
		t.Fatal(err)
	}
	sessionCtx := peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
		PeerCertificates: []*x509.Certificate{leaf}, VerifiedChains: [][]*x509.Certificate{{leaf}},
	}}})
	sessionCtx = metadata.NewIncomingContext(sessionCtx, metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
	var admittedSession context.Context
	err = s.gate.StreamInterceptor(nil, contextStream{ctx: sessionCtx}, &grpc.StreamServerInfo{FullMethod: action_servicepb.ActionService_WatchActionRequest_FullMethodName}, func(_ any, stream grpc.ServerStream) error {
		admittedSession = stream.Context()
		return nil
	})
	if err != nil {
		t.Fatalf("admit Session watch: %v", err)
	}
	if err := s.gate.Recheck(admittedSession); err != nil {
		t.Fatalf("Session watch failed before Identity replacement: %v", err)
	}
	if err := policyObjects.Delete(t.Context(), successor); err != nil {
		t.Fatal(err)
	}
	if err := policyObjects.Create(t.Context(), &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: successor.Name},
		Spec:       policyapi.IdentitySpec{Description: "second replacement Identity"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := s.gate.Recheck(admittedSession); err == nil {
		t.Fatal("Session watch survived same-name Identity replacement")
	}
}

// A service certificate proves the app transport actor. Only the separately verified
// operator token grants operator authority; a Kubernetes bearer never substitutes for it.
func TestServiceActorMustAlsoProveOperatorOIDC(t *testing.T) {
	s := serveWithGateOptions(t, func(options *grpcgate.Options, _ policyNamespace) {
		options.OperatorServices = []string{peertls.AppURI}
	})
	ca := transporttesting.NewCA(t)
	for _, tc := range []struct {
		name, actor, token string
		allowed            bool
	}{
		{"app and operator", peertls.AppURI, s.operatorToken, true},
		{"app alone", peertls.AppURI, "", false},
		{"app and Kubernetes bearer", peertls.AppURI, validKubernetesToken, false},
		{"ungranted actor with operator", peertls.NotificationsURI, s.operatorToken, false},
		{"expired operator", peertls.AppURI, s.issuer.Token(t, map[string]any{"exp": time.Now().Add(-time.Hour).Unix()}), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			files := ca.Issue(t, tc.actor)
			pair, err := tls.LoadX509KeyPair(files.CertFile, files.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			leaf, err := x509.ParseCertificate(pair.Certificate[0])
			if err != nil {
				t.Fatal(err)
			}
			ctx := peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}, VerifiedChains: [][]*x509.Certificate{{leaf}}}}})
			if tc.token != "" {
				ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer "+tc.token))
			}
			_, err = s.gate.UnaryInterceptor(ctx, nil, &grpc.UnaryServerInfo{FullMethod: operator_action_servicepb.OperatorActionService_DecideActionRequest_FullMethodName}, func(ctx context.Context, _ any) (any, error) {
				if _, ok := grpcgate.Admitted(ctx).Principal.(store.OperatorPrincipal); !ok {
					t.Fatalf("operator proof = %#v", grpcgate.Admitted(ctx).Principal)
				}
				return nil, nil
			})
			if tc.allowed {
				if err != nil {
					t.Fatal(err)
				}
			} else if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("unproved operator: %v", err)
			}
		})
	}
}

type contextStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s contextStream) Context() context.Context { return s.ctx }
