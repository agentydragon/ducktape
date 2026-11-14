package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestIdentitySVIDCannotClaimSameNamedAccountRequest(t *testing.T) {
	owner := workloadauthtesting.NewIdentityWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString(), policyNamespace).Workload
	other := workloadauthtesting.NewIdentityWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString(), policyNamespace).Workload
	set := "test-identity-set-" + uuid.NewString()
	if err := owner.Objects.Create(t.Context(), &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: set},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
			Type: string(store.PolicyExactActions), Actions: map[string]policyapi.ActionNames{"test-group": {"test-action"}},
		}}}},
	}); err != nil {
		t.Fatal(err)
	}
	for _, workload := range []workloadauthtesting.Workload{owner, other} {
		setCallerLabel(t, workload.Principal.Account, true)
		binding := &policyapi.PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-identity-binding-" + uuid.NewString()},
			Spec: policyapi.PolicyBindingSpec{
				Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: workload.Identity.Name}}, PolicySets: []string{set},
			},
		}
		if err := workload.Objects.Create(t.Context(), binding); err != nil {
			t.Fatal(err)
		}
	}
	ca := transporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, peertls.ActionsURI, x509.ExtKeyUsageServerAuth)
	service := startConfigured(t, kube.Config, &catalog.Catalog{}, func(c *config, _ *deps) {
		c.RPC = listener.Config{
			ListenAddress: "127.0.0.1:0",
			TLS:           &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile},
			Auth: []listener.Authentication{
				&listener.SPIFFEAuth{CAFile: serving.CAFile},
				&listener.KubernetesBearerAuth{Audiences: []string{testAudience}},
			},
		}
	})
	waitReady(t, service)
	_, err := service.store.Submit(t.Context(), store.CallerPrincipal{
		Account: store.ServiceAccount(owner.Principal.Account), ServiceAccountUID: owner.Principal.ServiceAccountUID,
	}, store.ActionRequestInput{
		IdempotencyKey: "svid-own-request", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{}`), Title: "owned request",
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	// The same TLS listener accepts a normal projected-token caller without a client certificate.
	bearerCredentials, err := (&peertls.ClientTLS{CAFile: serving.CAFile, ServerURI: peertls.ActionsURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	bearerConn, err := grpc.NewClient(service.address, grpc.WithTransportCredentials(bearerCredentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { bearerConn.Close() })
	bearerCtx, bearerCancel := context.WithTimeout(t.Context(), callDeadline)
	defer bearerCancel()
	bearerCtx = metadata.AppendToOutgoingContext(bearerCtx, "authorization", "Bearer "+readerToken)
	if _, err := action_servicepb.NewActionServiceClient(bearerConn).ListActionRequests(bearerCtx, &action_servicepb.ListActionRequestsRequest{}); err != nil {
		t.Fatalf("bearer call on the shared listener: %v", err)
	}
	for i, workload := range []workloadauthtesting.Workload{owner, other} {
		config := ca.Issue(t, workload.URI)
		transportCredentials, err := config.ClientCredentials(peertls.ActionsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.address, grpc.WithTransportCredentials(transportCredentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		response, err := action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
		if err != nil || len(response.GetActionRequests()) != 0 {
			t.Fatalf("Identity caller %s claimed a same-named ServiceAccount request: %v, %v", workload.Identity.Name, response, err)
		}
		if i == 0 {
			mixed := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken)
			if _, err := action_servicepb.NewActionServiceClient(conn).ListActionRequests(mixed, &action_servicepb.ListActionRequestsRequest{}); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("SVID plus bearer was accepted: %v", err)
			}
			operatorWatch, err := operator_action_servicepb.NewOperatorActionServiceClient(conn).WatchMcpServers(ctx, &operator_action_servicepb.WatchMcpServersRequest{})
			if err == nil {
				_, err = operatorWatch.Recv()
			}
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("SVID was accepted by an operator method: %v", err)
			}
			watch, err := action_servicepb.NewActionServiceClient(conn).WatchActionRequests(ctx, &action_servicepb.WatchActionRequestsRequest{})
			if err != nil {
				t.Fatal(err)
			}
			for {
				frame, err := watch.Recv()
				if err != nil {
					t.Fatal(err)
				}
				if frame.GetSnapshotComplete() != nil {
					break
				}
			}
			workload.Delete(t)
			_, err = action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox on existing TLS connection: %v", err)
			}
			if _, err := watch.Recv(); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Sandbox on existing watch: %v", err)
			}
		}
	}
	// The native gRPC TLS server rejects these leaves before its ActionService
	// interceptor can inspect a request or a forged bearer.
	valid := ca.Issue(t, other.URI)
	for name, leaf := range map[string]peertls.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, other.URI),
		"expired": ca.IssueWithValidity(t, other.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	} {
		t.Run(name, func(t *testing.T) {
			security, err := valid.ClientTLSConfig(peertls.ActionsURI)
			if err != nil {
				t.Fatal(err)
			}
			pair, err := tls.LoadX509KeyPair(leaf.CertFile, leaf.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			security.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			conn, err := grpc.NewClient(service.address, grpc.WithTransportCredentials(credentials.NewTLS(security)))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
			defer cancel()
			_, err = action_servicepb.NewActionServiceClient(conn).ListActionRequests(
				metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken),
				&action_servicepb.ListActionRequestsRequest{},
			)
			if status.Code(err) != codes.Unavailable {
				t.Fatalf("invalid leaf failed after transport admission: %v", err)
			}
		})
	}
}

// An Identity-subject SVID caller is admitted while a PolicyBinding names its Identity, is decided by those bindings,
// and owns what it submits as that Identity.
func TestSVIDIdentityCallerActsByItsIdentityBindings(t *testing.T) {
	bound := workloadauthtesting.NewIdentityWorkload(t, kube, policyNamespace, "identity-"+uuid.NewString(), policyNamespace)
	unbound := workloadauthtesting.NewIdentityWorkload(t, kube, policyNamespace, "identity-"+uuid.NewString(), policyNamespace)
	set := "test-identity-set-" + uuid.NewString()
	for _, object := range []client.Object{
		&policyapi.PolicySet{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: set},
			Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
				Type: string(store.PolicyExactActions), Actions: map[string]policyapi.ActionNames{"test-mcp": {"test_echo"}},
			}}}},
		},
		&policyapi.PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-identity-binding-" + uuid.NewString()},
			Spec: policyapi.PolicyBindingSpec{
				Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: bound.Identity.Name}}, PolicySets: []string{set},
			},
		},
	} {
		if err := bound.Objects.Create(t.Context(), object); err != nil {
			t.Fatal(err)
		}
	}
	groups := newCatalog(t, `{"test-mcp": `+mcpGroup(echoUpstream(t, new(atomic.Bool)))+`}`)
	ca := transporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, peertls.ActionsURI, x509.ExtKeyUsageServerAuth)
	service := startConfigured(t, kube.Config, groups, func(c *config, _ *deps) {
		c.ConnectionDelegationService = peertls.MCPEdgeURI
		c.RPC = listener.Config{
			ListenAddress: "127.0.0.1:0",
			TLS:           &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile},
			Auth: []listener.Authentication{
				&listener.SPIFFEAuth{CAFile: serving.CAFile},
				&listener.KubernetesBearerAuth{Audiences: []string{testAudience}},
			},
		}
	})
	waitReady(t, service)
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool {
		return h.GetState() == action_servicepb.McpLifecycle_MCP_LIFECYCLE_AVAILABLE
	})
	// A ServiceAccount's request, which no Identity reads.
	if _, err := service.store.Submit(t.Context(), store.CallerPrincipal{
		Account: store.ServiceAccount{Namespace: policyNamespace, Name: "test-identity-neighbour"}, ServiceAccountUID: "test-neighbour-uid",
	}, store.ActionRequestInput{
		IdempotencyKey: "test-neighbour", Action: store.ActionIdentity{Group: "test-mcp", Name: "test_echo"},
		Arguments: json.RawMessage(`{}`), Title: "test neighbour request",
	}, store.Admission{RequestID: uuid.New()}); err != nil {
		t.Fatal(err)
	}
	connect := func(workload workloadauthtesting.IdentityWorkload) action_servicepb.ActionServiceClient {
		transportCredentials, err := ca.Issue(t, workload.URI).ClientCredentials(peertls.ActionsURI)
		if err != nil {
			t.Fatal(err)
		}
		conn, err := grpc.NewClient(service.address, grpc.WithTransportCredentials(transportCredentials))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		return action_servicepb.NewActionServiceClient(conn)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()

	if _, err := connect(unbound).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Errorf("an Identity no binding names: %v, want UNAUTHENTICATED", err)
	}

	caller := connect(bound)
	var submitted *action_servicepb.SubmitActionRequestResponse
	// The index may not yet hold the binding the caller is admitted by.
	if err := wait.PollUntilContextTimeout(ctx, 20*time.Millisecond, callDeadline, true, func(ctx context.Context) (bool, error) {
		var err error
		submitted, err = caller.SubmitActionRequest(ctx, &action_servicepb.SubmitActionRequestRequest{
			IdempotencyKey: "test-identity-submit", Action: &action_servicepb.ActionIdentity{Group: "test-mcp", Name: "test_echo"},
			Arguments: &structpb.Struct{}, Title: "test identity request",
		})
		return status.Code(err) != codes.Unauthenticated, err
	}); err != nil {
		t.Fatalf("the bound Identity's submission: %v", err)
	}
	receipt := submitted.GetActionRequest()
	wantCaller := &identitypb.Identity{Name: bound.Subject.Identity.Name, Uid: bound.Subject.Identity.UID}
	if !proto.Equal(receipt.GetCallerIdentity(), wantCaller) || receipt.GetCaller() != nil ||
		receipt.GetDecision().GetPolicy() == nil || receipt.GetState() == action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING {
		t.Errorf("the receipt %v, want one an Identity binding allowed for %v", receipt, wantCaller)
	}
	listed, err := caller.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
	if err != nil || len(listed.GetActionRequests()) != 1 || listed.GetActionRequests()[0].GetId() != receipt.GetId() {
		t.Errorf("the Identity lists %v, %v; want its own request alone", listed, err)
	}
	policy, err := caller.GetActionPolicy(ctx, &action_servicepb.GetActionPolicyRequest{})
	if err != nil || !proto.Equal(policy.GetActionPolicy().GetIdentity(), wantCaller) || len(policy.GetActionPolicy().GetAutoApproveIf()) != 1 {
		t.Errorf("the Identity's own policy: %v, %v", policy, err)
	}
	transportCredentials, err := ca.Issue(t, peertls.MCPEdgeURI).ClientCredentials(peertls.ActionsURI)
	if err != nil {
		t.Fatal(err)
	}
	edgeConn, err := grpc.NewClient(service.address, grpc.WithNoProxy(), grpc.WithTransportCredentials(transportCredentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = edgeConn.Close() })
	edgeClient := action_servicepb.NewActionServiceClient(edgeConn)
	for _, subject := range []*identitypb.Identity{wantCaller, {Name: unbound.Identity.Name, Uid: string(unbound.Identity.UID)}, {Name: wantCaller.Name, Uid: uuid.NewString()}} {
		envelope := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: subject}, Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{ConnectionId: uuid.NewString(), GrantId: uuid.NewString()}}}
		encoded, err := auth.MarshalDelegation(envelope)
		if err != nil {
			t.Fatal(err)
		}
		delegated := metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, string(encoded))
		result, err := edgeClient.SubmitActionRequest(delegated, &action_servicepb.SubmitActionRequestRequest{IdempotencyKey: uuid.NewString(), Action: &action_servicepb.ActionIdentity{Group: "test-mcp", Name: "test_echo"}, Arguments: &structpb.Struct{}, Title: "delegated service request"})
		if proto.Equal(subject, wantCaller) {
			if err != nil || !proto.Equal(result.GetActionRequest().GetCallerIdentity(), wantCaller) || result.GetActionRequest().GetCaller() != nil {
				t.Fatalf("service actor effective caller=%v,%v", result, err)
			}
		} else if status.Code(err) != codes.Unauthenticated && status.Code(err) != codes.PermissionDenied {
			t.Fatalf("service actor admitted ungranted/stale Identity: %v", err)
		}
	}
}

func TestServiceReaderAdmissionIsReadOnlyAndNotAnOperator(t *testing.T) {
	ca := transporttesting.NewCA(t)
	serving := ca.Issue(t, peertls.ActionsURI)
	service := startConfigured(t, kube.Config, &catalog.Catalog{}, func(c *config, _ *deps) {
		c.ServiceReaders = []string{peertls.NotificationsURI}
		c.RPC = listener.Config{ListenAddress: "127.0.0.1:0", TLS: &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}, Auth: []listener.Authentication{&listener.SPIFFEAuth{CAFile: serving.CAFile}, &listener.KubernetesBearerAuth{Audiences: []string{testAudience}}}}
	})
	waitReady(t, service)
	for _, actor := range []string{peertls.NotificationsURI, peertls.MCPEdgeURI} {
		t.Run(actor, func(t *testing.T) {
			security, err := ca.Issue(t, actor).ClientCredentials(peertls.ActionsURI)
			if err != nil {
				t.Fatal(err)
			}
			conn, err := grpc.NewClient(service.address, grpc.WithNoProxy(), grpc.WithTransportCredentials(security))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			client := action_servicepb.NewActionServiceClient(conn)
			ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
			defer cancel()
			_, err = client.ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
			if actor == peertls.NotificationsURI {
				if err != nil {
					t.Fatal(err)
				}
			} else if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("unlisted service read: %v", err)
			}
			if _, err := client.SubmitActionRequest(ctx, &action_servicepb.SubmitActionRequestRequest{}); status.Code(err) != codes.PermissionDenied && status.Code(err) != codes.Unauthenticated {
				t.Fatalf("service mutated: %v", err)
			}
			mixed := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken)
			if _, err := client.ListActionRequests(mixed, &action_servicepb.ListActionRequestsRequest{}); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("bearer fallback: %v", err)
			}
			operatorWatch, err := operator_action_servicepb.NewOperatorActionServiceClient(conn).WatchMcpServers(ctx, &operator_action_servicepb.WatchMcpServersRequest{})
			if err == nil {
				_, err = operatorWatch.Recv()
			}
			if status.Code(err) != codes.Unauthenticated {
				t.Fatalf("service acquired operator grant: %v", err)
			}
		})
	}
}
