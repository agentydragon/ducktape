// Package bearer dials Agentplane services as a workload: each call carries the Pod's projected ServiceAccount
// token, read again for the call because the kubelet rotates it (agentplane/docs/grpc_services.md).
package bearer

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	tokenFileReadError  = "could not read the bearer token file"
	tokenFileEmptyError = "the bearer token file is empty"
)

// Dial opens a connection to target whose calls carry the token in tokenPath; unary calls end after timeout. It
// never retries: a mutation whose outcome is unknown needs its caller's reconciliation, not a repeat. It does
// not connect until the first call, and options come after, so they may override these.
func Dial(target, tokenPath string, timeout time.Duration, options ...grpc.DialOption) (*grpc.ClientConn, error) {
	if target == "" || tokenPath == "" || timeout <= 0 {
		return nil, errors.New("target, token file and a positive timeout are required")
	}
	return grpc.NewClient(target, append([]grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		observability.GRPCClientOption(),
		grpc.WithUnaryInterceptor(withBearerTokenAndTimeout(tokenPath, timeout)),
		grpc.WithStreamInterceptor(withBearerToken(tokenPath)),
		grpc.WithDisableRetry(),
	}, options...)...)
}

// IsTokenFileError reports whether a call failed before sending because the projected bearer could not be read.
func IsTokenFileError(err error) bool {
	var tokenErr *tokenFileError
	return errors.As(err, &tokenErr)
}

type tokenFileError struct{ message string }

func (e *tokenFileError) Error() string { return e.message }

func (e *tokenFileError) GRPCStatus() *status.Status { return status.New(codes.Unavailable, e.message) }

func readToken(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", &tokenFileError{message: tokenFileReadError}
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return "", &tokenFileError{message: tokenFileEmptyError}
	}
	return token, nil
}

func withToken(ctx context.Context, path string) (context.Context, error) {
	token, err := readToken(path)
	if err != nil {
		return nil, err
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token), nil
}

func withBearerTokenAndTimeout(tokenPath string, timeout time.Duration) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoker grpc.UnaryInvoker, options ...grpc.CallOption) error {
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		var err error
		if ctx, err = withToken(ctx, tokenPath); err != nil {
			return err
		}
		return invoker(ctx, method, request, reply, connection, options...)
	}
}

func withBearerToken(tokenPath string) grpc.StreamClientInterceptor {
	return func(ctx context.Context, description *grpc.StreamDesc, connection *grpc.ClientConn, method string, streamer grpc.Streamer, options ...grpc.CallOption) (grpc.ClientStream, error) {
		ctx, err := withToken(ctx, tokenPath)
		if err != nil {
			return nil, err
		}
		return streamer(ctx, description, connection, method, options...)
	}
}
