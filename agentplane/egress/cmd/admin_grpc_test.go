package main

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const testAudience = "test-audience"

var (
	api *kubeapiserver.Server
	// testToken proves the ServiceAccount test-ns/test-app for testAudience.
	testToken string
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		api = server
		testToken, err = server.WorkloadToken(context.Background(), auth.Account{Namespace: "test-ns", Name: "test-app"}, testAudience)
		return err
	}))
}

type noDecisions struct{}

func (noDecisions) Recent(context.Context, *decision.Subject) ([]decision.Record, error) {
	return []decision.Record{}, nil
}

func (noDecisions) ByEventIDs(context.Context, []uuid.UUID) ([]decision.Record, error) {
	return []decision.Record{}, nil
}

func (noDecisions) Readable(context.Context) error { return nil }

func (noDecisions) Window() history.Window {
	return history.Window{Retention: 24 * time.Hour, Capacity: history.MaxCapacity}
}

// quietChanges reports once on subscribing and then nothing.
type quietChanges struct{}

func (quietChanges) Follow(ctx context.Context, _ *decision.Subject, changed func(history.Change) error) error {
	if err := changed(history.Initial{}); err != nil {
		return err
	}
	<-ctx.Done()
	return ctx.Err()
}

type readyIndex struct{}

func (readyIndex) Status() watchhealth.Status {
	return watchhealth.Status{Available: true, Synced: true}
}

type idleHistory struct{}

func (idleHistory) Health() history.Health { return history.Health{} }

func testRulesAuth(t *testing.T) (*identity.Authenticator, *policy.Index) {
	t.Helper()
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{"test-ns"})
	if err != nil {
		t.Fatal(err)
	}
	forward := listener.Config{
		ListenAddress: "127.0.0.1:0",
		Auth:          []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{testAudience}}},
	}
	authentication, err := identity.NewAuthenticator(forward, identity.NewVerifier(resolver, nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	return authentication, policy.NewIndex()
}

func TestAdminListenerServesGRPCAndShutsDownWithAnOpenWatch(t *testing.T) {
	c := config{AdminGRPCAudience: testAudience, AdminGRPCCallers: []string{"test-ns/test-app"}}
	authentication, index := testRulesAuth(t)
	server, err := newAdminServer(c, api.TokenReviews(), admin.ServiceOptions{
		Decisions: noDecisions{}, Changes: quietChanges{}, HistoryHealth: idleHistory{}, Index: readyIndex{},
	}, authentication, index)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- serving("admin", server.Serve(listener)) }()

	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	probe, err := healthpb.NewHealthClient(connection).Check(context.Background(), &healthpb.HealthCheckRequest{})
	if err != nil || probe.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("health probe: %v, %v", probe, err)
	}
	admitted := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+testToken)
	health, err := egressadminpb.NewEgressAdminClient(connection).GetHealth(admitted, &egressadminpb.GetHealthRequest{})
	if err != nil || !health.GetReady() {
		t.Fatalf("GetHealth: %v, %v", health, err)
	}
	// The same listener serves workload rules without granting the workload admin access.
	workloadToken, err := api.WorkloadToken(context.Background(), auth.Account{Namespace: "test-ns", Name: "test-sandbox"}, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	workload := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+workloadToken)
	rules, err := egressrulespb.NewEgressRulesClient(connection).GetRules(workload, &egressrulespb.GetRulesRequest{})
	if err != nil || rules.GetSubject().GetNamespace() != "test-ns" || rules.GetSubject().GetName() != "test-sandbox" {
		t.Fatalf("workload rules: %v, %v", rules, err)
	}
	if _, err := egressadminpb.NewEgressAdminClient(connection).GetHealth(workload, &egressadminpb.GetHealthRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("workload admin access: %v, want PermissionDenied", err)
	}
	if _, err := egressrulespb.NewEgressRulesClient(connection).GetRules(context.Background(), &egressrulespb.GetRulesRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("anonymous rules access: %v, want Unauthenticated", err)
	}
	stream, err := egressadminpb.NewEgressAdminClient(connection).WatchDecisions(
		admitted,
		&egressadminpb.WatchDecisionsRequest{Filter: &egressadminpb.DecisionFilter{
			Scope: &egressadminpb.DecisionFilter_Unauthenticated{Unauthenticated: &egressadminpb.Unauthenticated{}},
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown with a watch open: %v", err)
	}
	if err := <-served; err != nil {
		t.Fatalf("a server shut down on purpose reported %v", err)
	}
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("the watch ended with %v, want a clean end", err)
	}
}

func TestAdminListenerServesRulesAndBackendAdministrationOverTLS(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	proxySVID := ca.Issue(t, environment_runtime_transport.EgressProxyURI)
	forward := listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: proxySVID.CertFile, KeyFile: proxySVID.KeyFile},
		Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: ca.RootFile}},
	}
	workloadAuth, err := identity.NewAuthenticator(forward, identity.NewVerifier(nil, nil), testResolver(t))
	if err != nil {
		t.Fatal(err)
	}
	server, err := newAdminServer(config{
		Forward: forward, AdminGRPCAudience: testAudience, AdminGRPCCallers: []string{"test-ns/test-app"},
	}, api.TokenReviews(), admin.ServiceOptions{
		Decisions: noDecisions{}, Changes: quietChanges{}, HistoryHealth: idleHistory{}, Index: readyIndex{},
	}, workloadAuth, policy.NewIndex())
	if err != nil {
		t.Fatal(err)
	}
	adminListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- serving("admin", server.Serve(adminListener)) }()
	stopped := false
	t.Cleanup(func() {
		if stopped {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
	})

	backendCredentials, err := (&environment_runtime_transport.ClientTLS{CAFile: ca.RootFile, ServerURI: environment_runtime_transport.EgressProxyURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	backend, err := grpc.NewClient(adminListener.Addr().String(), grpc.WithTransportCredentials(backendCredentials))
	if err != nil {
		t.Fatal(err)
	}
	defer backend.Close()
	probe, err := healthpb.NewHealthClient(backend).Check(context.Background(), &healthpb.HealthCheckRequest{})
	if err != nil || probe.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("TLS health probe: %v, %v", probe, err)
	}
	adminContext := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+testToken)
	adminHealth, err := egressadminpb.NewEgressAdminClient(backend).GetHealth(adminContext, &egressadminpb.GetHealthRequest{})
	if err != nil || !adminHealth.GetReady() {
		t.Fatalf("TLS admin API: %v, %v", adminHealth, err)
	}
	if _, err := egressrulespb.NewEgressRulesClient(backend).GetRules(adminContext, &egressrulespb.GetRulesRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Rules with a backend bearer: %v, want Unauthenticated", err)
	}

	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "test-ns", Name: "test-sandbox", CredentialID: runnerCredentialID}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runnerSVID := ca.Issue(t, runnerURI)
	runnerCredentials, err := runnerSVID.ClientCredentials(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	secured, err := grpc.NewClient(adminListener.Addr().String(), grpc.WithTransportCredentials(runnerCredentials))
	if err != nil {
		t.Fatal(err)
	}
	defer secured.Close()
	response, err := egressrulespb.NewEgressRulesClient(secured).GetRules(context.Background(), &egressrulespb.GetRulesRequest{})
	if err != nil || response.GetSubject().GetNamespace() != "test-ns" || response.GetSubject().GetName() != "test-sandbox" {
		t.Fatalf("SVID Rules response: %v, %v", response, err)
	}

	if _, err := egressadminpb.NewEgressAdminClient(secured).GetHealth(adminContext, &egressadminpb.GetHealthRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("SVID plus admin bearer: %v, want Unauthenticated", err)
	}
	plain, err := grpc.NewClient(adminListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer plain.Close()
	plainContext, plainCancel := context.WithTimeout(t.Context(), time.Second)
	defer plainCancel()
	if _, err := healthpb.NewHealthClient(plain).Check(plainContext, &healthpb.HealthCheckRequest{}); err == nil {
		t.Fatal("TLS RPC listener accepted plaintext")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	stopped = true
	if err := <-served; err != nil {
		t.Fatalf("server shut down on purpose reported %v", err)
	}
}

const runnerCredentialID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

// testResolver holds the record the Sandbox Controller and the issuer would write for an accepted
// runner Pod, and its subject.
func testResolver(t *testing.T) *resolver.Resolver {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	uri, err := environment_runtime_transport.WorkloadID{Namespace: "test-ns", Name: "test-sandbox", CredentialID: runnerCredentialID}.URI()
	if err != nil {
		t.Fatal(err)
	}
	pod := &workloadapi.Binding{PodUID: "00000000-0000-4000-8000-000000000001"}
	record := &workloadapi.WorkloadCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: "test-ns", Name: "test-sandbox", OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: "test-sandbox", UID: "00000000-0000-4000-8000-000000000002", Controller: new(true),
		}}},
		Spec: workloadapi.WorkloadCredentialSpec{
			Subject: workloadapi.Subject{ServiceAccount: "test-sandbox"}, Binding: pod,
			Pod: &workloadapi.PodRef{Name: "runner", UID: pod.PodUID},
		},
		Status: workloadapi.WorkloadCredentialStatus{CredentialID: runnerCredentialID, URI: uri, AcceptedBinding: pod},
	}
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: "test-ns", Name: "test-sandbox", UID: types.UID("test-account-uid")}}
	records, err := resolver.New(clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(record, account).Build(), []string{"test-ns"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return records
}

// Kubernetes' native grpc probes send no bearer, so health is served whether or not the EgressAdmin
// API is configured.
func TestAdminListenerServesHealthWithoutTheAdminGRPCAPI(t *testing.T) {
	authentication, index := testRulesAuth(t)
	server, err := newAdminServer(config{}, api.TokenReviews(), admin.ServiceOptions{Index: readyIndex{}}, authentication, index)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	defer func() { _ = server.Shutdown(context.Background()) }()

	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	probe, err := healthpb.NewHealthClient(connection).Check(context.Background(), &healthpb.HealthCheckRequest{})
	if err != nil || probe.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("health probe: %v, %v", probe, err)
	}
	_, err = egressadminpb.NewEgressAdminClient(connection).GetHealth(context.Background(), &egressadminpb.GetHealthRequest{})
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("GetHealth without the admin gRPC API configured: %v, want Unimplemented", err)
	}
}
