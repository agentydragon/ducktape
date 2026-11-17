package sessionclient

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
)

// SessionDispatcherConfig controls one fresh, non-retrying connection per unary
// Session Dispatcher call. A projected token is reread per RPC in bearer mode;
// service SVID mode sends no bearer metadata.
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
	if config.Target == "" || config.Timeout <= 0 {
		return nil, errors.New("Session Dispatcher target and positive timeout are required")
	}
	if err := config.TLS.ValidateCaller(config.TokenFile, peertls.AppURI); err != nil {
		return nil, errors.New("Session Dispatcher caller authentication is invalid")
	}
	return &SessionDispatcherClient{config: config}, nil
}

func (c *SessionDispatcherClient) connection(ctx context.Context, call func(context.Context, dispatcherpb.SessionDispatcherClient) error) error {
	conn, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout, c.config.TLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	return call(ctx, dispatcherpb.NewSessionDispatcherClient(conn))
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
	return response, translatedRPCError(err)
}

// GetSession reads the Dispatcher's current activity projection once for App mutation policy.
func (c *SessionDispatcherClient) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	var response *dispatcherpb.GetSessionResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.GetSession(ctx, request)
		return err
	})
	return response, err
}

var _ SessionDispatcher = (*SessionDispatcherClient)(nil)
