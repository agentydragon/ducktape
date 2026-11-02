package stream

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
	"google.golang.org/grpc/backoff"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

// NewConnection opens the Runner's shared outbound gRPC connection.
func NewConnection(target string) (*grpc.ClientConn, error) {
	return NewSecureConnection(target, insecure.NewCredentials())
}

// NewSecureConnection opens a shared outbound gRPC connection with the supplied transport identity.
func NewSecureConnection(target string, transport credentials.TransportCredentials) (*grpc.ClientConn, error) {
	return grpc.NewClient(target, observability.GRPCClientOption(), grpc.WithTransportCredentials(transport), grpc.WithNoProxy(), grpc.WithConnectParams(grpc.ConnectParams{Backoff: backoff.Config{BaseDelay: 500 * time.Millisecond, Multiplier: 2, Jitter: 0.2, MaxDelay: 30 * time.Second}, MinConnectTimeout: 10 * time.Second}))
}

// Authenticated adds the bearer used by the Runner's outbound gRPC services.
func Authenticated(ctx context.Context, bearer string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearer)
}
