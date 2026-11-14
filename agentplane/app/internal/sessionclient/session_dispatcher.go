package sessionclient

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
)

// SessionDispatcherConfig controls one fresh, non-retrying connection per unary
// Session Dispatcher call. Projected bearer tokens are read by bearer.Dial for
// each RPC, so kubelet token rotation is observed without restarting the app.
type SessionDispatcherConfig struct {
	TLS       *peertls.ClientTLS
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// SessionDispatcherClient is the generated Session Dispatcher transport adapter.
type SessionDispatcherClient struct{ config SessionDispatcherConfig }

// NewSessionDispatcherClient validates a Session Dispatcher transport configuration.
func NewSessionDispatcherClient(config SessionDispatcherConfig) (*SessionDispatcherClient, error) {
	if config.Target == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("Session Dispatcher target, token file, and positive timeout are required")
	}
	return &SessionDispatcherClient{config: config}, nil
}

func (c *SessionDispatcherClient) connection(ctx context.Context, call func(context.Context, dispatcherpb.SessionDispatcherClient) error) error {
	conn, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout, c.config.TLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	return translatedRPCError(call(ctx, dispatcherpb.NewSessionDispatcherClient(conn)))
}

// CreateSession calls the durable, idempotent Session RPC exactly once.
// A RequestTimeoutError indicates that callers must reconcile or retry with
// the same Session ID.
func (c *SessionDispatcherClient) CreateSession(ctx context.Context, request *dispatcherpb.CreateSessionRequest) (*dispatcherpb.CreateSessionResponse, error) {
	var response *dispatcherpb.CreateSessionResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.CreateSession(ctx, request)
		return err
	})
	return response, err
}

var _ SessionDispatcher = (*SessionDispatcherClient)(nil)
