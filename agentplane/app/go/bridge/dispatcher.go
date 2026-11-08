package bridge

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
)

// DispatcherConfig controls one fresh, non-retrying connection per unary
// Session Dispatcher call. Projected bearer tokens are read by bearer.Dial for
// each RPC, so kubelet token rotation is observed without restarting the app.
type DispatcherConfig struct {
	TLS       *environment_runtime_transport.ClientTLS
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// DispatcherClient is the generated Session Dispatcher transport adapter.
type DispatcherClient struct{ config DispatcherConfig }

// NewDispatcherClient validates a dispatcher transport configuration.
func NewDispatcherClient(config DispatcherConfig) (*DispatcherClient, error) {
	if config.Target == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("Session Dispatcher target, token file, and positive timeout are required")
	}
	return &DispatcherClient{config: config}, nil
}

func (c *DispatcherClient) connection(ctx context.Context, call func(context.Context, dispatcherpb.SessionDispatcherClient) error) error {
	conn, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout, c.config.TLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	return translatedRPCError(call(ctx, dispatcherpb.NewSessionDispatcherClient(conn)))
}

// CreateSession calls the durable, idempotent Session-session RPC exactly once.
// A RequestTimeoutError indicates that callers must reconcile or retry with
// the same Session ID.
func (c *DispatcherClient) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	var response *dispatcherpb.CreateSessionResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.CreateSession(ctx, request)
		return err
	})
	return response, err
}

var _ Dispatcher = (*DispatcherClient)(nil)
