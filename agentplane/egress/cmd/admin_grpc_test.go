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
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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

func testRulesAuth(t *testing.T) (*auth.Resolver, *identity.Verifier, *policy.Index) {
	t.Helper()
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{"test-ns"})
	if err != nil {
		t.Fatal(err)
	}
	return resolver, identity.NewVerifier(resolver, nil), policy.NewIndex()
}

func TestAdminListenerServesGRPCAndShutsDownWithAnOpenWatch(t *testing.T) {
	c := config{AdminGRPCAudience: testAudience, AdminGRPCCallers: []string{"test-ns/test-app"}}
	resolver, verifier, index := testRulesAuth(t)
	server, err := newAdminServer(c, api.TokenReviews(), admin.ServiceOptions{
		Decisions: noDecisions{}, Changes: quietChanges{}, HistoryHealth: idleHistory{}, Index: readyIndex{},
	}, resolver, verifier, index)
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

// Kubernetes' native grpc probes send no bearer, so health is served whether or not the EgressAdmin
// API is configured.
func TestAdminListenerServesHealthWithoutTheAdminGRPCAPI(t *testing.T) {
	resolver, verifier, index := testRulesAuth(t)
	server, err := newAdminServer(config{}, api.TokenReviews(), admin.ServiceOptions{Index: readyIndex{}}, resolver, verifier, index)
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
