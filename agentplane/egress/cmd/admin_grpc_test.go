package main

import (
	"context"
	"errors"
	"io"
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/proto/egress_admin"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
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

func TestShutdownEndsAnOpenWatchInsteadOfWaitingOutItsLease(t *testing.T) {
	c := config{AdminGRPCAudience: "test-audience", AdminGRPCCallers: []string{"test-ns/test-app"}}
	adminGRPCServer, err := newAdminGRPC(c, acceptingReviews{}, noDecisions{}, quietChanges{})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- serving("admin gRPC", adminGRPCServer.server.Serve(listener)) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	stream, err := egressadminpb.NewEgressAdminClient(connection).WatchDecisions(
		metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+testToken),
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
	if err := adminGRPCServer.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if err := <-served; err != nil {
		t.Fatalf("a server stopped on purpose reported %v", err)
	}
	if _, err := stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("the watch ended with %v, want a clean end", err)
	}
}
