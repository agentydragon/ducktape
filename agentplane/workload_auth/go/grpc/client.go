// Package workloadgrpc builds delegated gRPC calls without the ordinary Bearer fallback.
package workloadgrpc

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const maxTokenSize = 16384

var ErrDelegationContext = errors.New("delegation context is missing or duplicated")

type delegationContextKey struct{}

// WithDelegation adds one validated delegation envelope to the call context. It rejects raw auth
// metadata so the client cannot accidentally combine delegated credentials with Bearer auth.
func WithDelegation(ctx context.Context, envelope *delegation.Delegation) (context.Context, error) {
	if _, exists := ctx.Value(delegationContextKey{}).([]byte); exists {
		return nil, ErrDelegationContext
	}
	metadata, _ := metadata.FromOutgoingContext(ctx)
	if len(metadata.Get("authorization")) != 0 || len(metadata.Get(auth.DelegationMetadataKey)) != 0 {
		return nil, ErrDelegationContext
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		return nil, err
	}
	return context.WithValue(ctx, delegationContextKey{}, encoded), nil
}

// DialDelegated calls target using the destination-audience token at tokenPath and an explicit
// per-call Delegation envelope. It has no ordinary Bearer interceptor and disables retries.
// The token file is read for each RPC so projected-token rotation is observed.
func DialDelegated(target, tokenPath string, timeout time.Duration) (*grpc.ClientConn, error) {
	if target == "" || tokenPath == "" || timeout <= 0 {
		return nil, errors.New("target, token file and a positive timeout are required")
	}
	return grpc.NewClient(
		target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		observability.GRPCClientOption(),
		grpc.WithPerRPCCredentials(delegatedCredentials{tokenPath: tokenPath}),
		grpc.WithUnaryInterceptor(withTimeout(timeout)),
		grpc.WithDisableRetry(),
	)
}

type delegatedCredentials struct{ tokenPath string }

func (c delegatedCredentials) GetRequestMetadata(ctx context.Context, _ ...string) (map[string]string, error) {
	encoded, ok := ctx.Value(delegationContextKey{}).([]byte)
	if !ok || len(encoded) == 0 {
		return nil, ErrDelegationContext
	}
	metadata, _ := metadata.FromOutgoingContext(ctx)
	if len(metadata.Get("authorization")) != 0 || len(metadata.Get(auth.DelegationMetadataKey)) != 0 {
		return nil, ErrDelegationContext
	}
	data, err := os.ReadFile(c.tokenPath)
	if err != nil {
		return nil, &tokenFileError{message: "could not read the delegated bearer token file"}
	}
	token := strings.TrimSpace(string(data))
	if token == "" || len(token) > maxTokenSize || strings.ContainsAny(token, " \t\r\n") {
		return nil, &tokenFileError{message: "the delegated bearer token file is empty or invalid"}
	}
	return map[string]string{
		"authorization":            "Agentplane-Delegated " + token,
		auth.DelegationMetadataKey: string(encoded),
	}, nil
}

func (delegatedCredentials) RequireTransportSecurity() bool { return false }

type tokenFileError struct{ message string }

func (e *tokenFileError) Error() string { return e.message }

func (e *tokenFileError) GRPCStatus() *status.Status { return status.New(codes.Unavailable, e.message) }

func withTimeout(timeout time.Duration) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoker grpc.UnaryInvoker, options ...grpc.CallOption) error {
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		return invoker(ctx, method, request, reply, connection, options...)
	}
}

var _ credentials.PerRPCCredentials = delegatedCredentials{}
