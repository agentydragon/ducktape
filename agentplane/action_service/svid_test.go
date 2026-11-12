package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
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

func TestSVIDListenerPreservesCallerLabelsAndRequestOwnership(t *testing.T) {
	owner := workloadauthtesting.NewWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString())
	other := workloadauthtesting.NewWorkload(t, kube, policyNamespace, "svid-"+uuid.NewString())
	for _, workload := range []workloadauthtesting.Workload{owner, other} {
		setCallerLabel(t, workload.Principal.Account, true)
	}
	ca := transporttesting.NewCA(t)
	serving := ca.IssueWithUsage(t, environment_runtime_transport.ActionsURI, x509.ExtKeyUsageServerAuth)
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
	bearerCredentials, err := (&environment_runtime_transport.ClientTLS{CAFile: serving.CAFile, ServerURI: environment_runtime_transport.ActionsURI}).Credentials()
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
		transportCredentials, err := config.ClientCredentials(environment_runtime_transport.ActionsURI)
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
		if err != nil || len(response.GetActionRequests()) != 1-i {
			t.Fatalf("SVID caller %s reads %v, error %v", workload.Sandbox.Name, response, err)
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
		} else {
			setCallerLabel(t, workload.Principal.Account, false)
			if err := wait.PollUntilContextTimeout(ctx, 20*time.Millisecond, 5*time.Second, true, func(ctx context.Context) (bool, error) {
				_, err := action_servicepb.NewActionServiceClient(conn).ListActionRequests(ctx, &action_servicepb.ListActionRequestsRequest{})
				return status.Code(err) == codes.Unauthenticated, nil
			}); err != nil {
				t.Fatal("SVID caller retained access after caller-label removal", err)
			}
		}
	}
	// The native gRPC TLS server rejects these leaves before its ActionService
	// interceptor can inspect a request or a forged bearer.
	valid := ca.Issue(t, other.URI)
	for name, leaf := range map[string]environment_runtime_transport.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, other.URI),
		"expired": ca.IssueWithValidity(t, other.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	} {
		t.Run(name, func(t *testing.T) {
			security, err := valid.ClientTLSConfig(environment_runtime_transport.ActionsURI)
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
	serving := ca.IssueWithUsage(t, environment_runtime_transport.ActionsURI, x509.ExtKeyUsageServerAuth)
	service := startConfigured(t, kube.Config, groups, func(c *config, _ *deps) {
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
		transportCredentials, err := ca.Issue(t, workload.URI).ClientCredentials(environment_runtime_transport.ActionsURI)
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
}
