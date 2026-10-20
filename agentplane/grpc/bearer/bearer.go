// Package bearer dials Agentplane services as a workload: each call carries the Pod's projected ServiceAccount
// token, read again for the call because the kubelet rotates it (agentplane/docs/grpc_services.md).
package bearer

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

// tokenFile is the credentials of the token file at a path.
type tokenFile string

func (f tokenFile) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	data, err := os.ReadFile(string(f))
	if err != nil {
		return nil, status.Error(codes.Unavailable, "could not read the bearer token file")
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return nil, status.Error(codes.Unavailable, "the bearer token file is empty")
	}
	return map[string]string{"authorization": "Bearer " + token}, nil
}

// RequireTransportSecurity is false: services in the cluster speak cleartext HTTP/2, and the token is audience-bound.
func (tokenFile) RequireTransportSecurity() bool { return false }

// Dial opens a connection to target whose unary calls carry the token in tokenPath and end after timeout. It
// never retries: a mutation whose outcome is unknown needs its caller's reconciliation, not a repeat. It does
// not connect until the first call, and options come after, so they may override these.
func Dial(target, tokenPath string, timeout time.Duration, options ...grpc.DialOption) (*grpc.ClientConn, error) {
	if target == "" || tokenPath == "" || timeout <= 0 {
		return nil, errors.New("target, token file and a positive timeout are required")
	}
	return grpc.NewClient(target, append([]grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithPerRPCCredentials(tokenFile(tokenPath)),
		grpc.WithUnaryInterceptor(withTimeout(timeout)),
		grpc.WithDisableRetry(),
	}, options...)...)
}

func withTimeout(timeout time.Duration) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoker grpc.UnaryInvoker, options ...grpc.CallOption) error {
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		return invoker(ctx, method, request, reply, connection, options...)
	}
}
