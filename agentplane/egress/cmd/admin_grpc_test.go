package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const testToken = "test-admin-token"

// acceptingReviews proves testToken to the ServiceAccount test-ns/test-app.
type acceptingReviews struct{}

func (acceptingReviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: review.Spec.Token == testToken,
		Audiences:     review.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:test-ns:test-app", UID: "test-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"test-pod"}, "authentication.kubernetes.io/pod-uid": {"test-pod-uid"},
			},
		},
	}}, nil
}

type noDecisions struct{}

func (noDecisions) Recent(context.Context, *decision.Subject) ([]decision.Record, error) {
	return []decision.Record{}, nil
}

// quietChanges reports once on subscribing and then nothing.
type quietChanges struct{}

func (quietChanges) Follow(ctx context.Context, changed func() error) error {
	if err := changed(); err != nil {
		return err
	}
	<-ctx.Done()
	return ctx.Err()
}

type readyIndex struct{}

func (readyIndex) Status(time.Time) policy.Status {
	return policy.Status{Available: true, Synced: true}
}

type idleHistory struct{}

func (idleHistory) Health() history.Health { return history.Health{} }

func TestAdminListenerServesHTTPAndGRPCAndShutsDownWithAnOpenWatch(t *testing.T) {
	c := config{AdminGRPCAudience: "test-audience", AdminGRPCCallers: []string{"test-ns/test-app"}}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /test-live", func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "alive") })
	adminServer := &http.Server{Handler: mux}
	shutdown, err := mountAdminGRPC(adminServer, c, acceptingReviews{}, admin.ServiceOptions{
		Decisions: noDecisions{}, Changes: quietChanges{}, HistoryHealth: idleHistory{}, Index: readyIndex{}, Clock: time.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- serving("admin", adminServer.Serve(listener)) }()

	// HTTP/1.1 and cleartext HTTP/2 that is not gRPC reach the HTTP handler.
	var h2c http.Protocols
	h2c.SetUnencryptedHTTP2(true)
	for name, client := range map[string]*http.Client{
		"HTTP/1.1": http.DefaultClient, "h2c": {Transport: &http.Transport{Protocols: &h2c}},
	} {
		response, err := client.Get("http://" + listener.Addr().String() + "/test-live")
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		body, err := io.ReadAll(response.Body)
		_ = response.Body.Close()
		if err != nil || string(body) != "alive" {
			t.Fatalf("%s: %q, %v", name, body, err)
		}
	}

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
	if err := shutdown.Shutdown(ctx); err != nil {
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
	adminServer := &http.Server{Handler: http.NewServeMux()}
	shutdown, err := mountAdminGRPC(adminServer, config{}, acceptingReviews{}, admin.ServiceOptions{Index: readyIndex{}, Clock: time.Now})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = adminServer.Serve(listener) }()
	defer func() { _ = shutdown.Shutdown(context.Background()) }()

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
