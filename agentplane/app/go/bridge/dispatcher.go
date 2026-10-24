package bridge

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
)

// DispatcherConfig controls one fresh, non-retrying connection per unary
// Session Dispatcher call. Projected bearer tokens are read by bearer.Dial for
// each RPC, so kubelet token rotation is observed without restarting the app.
type DispatcherConfig struct {
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
	conn, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	return translatedRPCError(call(ctx, dispatcherpb.NewSessionDispatcherClient(conn)))
}

// SubmitInput admits one input command. Retries must reuse its command ID.
func (c *DispatcherClient) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	var response *dispatcherpb.SubmitInputResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.SubmitInput(ctx, request)
		return err
	})
	return response, err
}

// ChangeModel admits one model change. Retries must reuse its command ID.
func (c *DispatcherClient) ChangeModel(ctx context.Context, request *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error) {
	var response *dispatcherpb.ChangeModelResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.ChangeModel(ctx, request)
		return err
	})
	return response, err
}

// ChangeReasoningEffort admits one reasoning-effort change. Retries must reuse
// its command ID.
func (c *DispatcherClient) ChangeReasoningEffort(ctx context.Context, request *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error) {
	var response *dispatcherpb.ChangeReasoningEffortResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.ChangeReasoningEffort(ctx, request)
		return err
	})
	return response, err
}

// InterruptTurn sends one identified turn interruption.
func (c *DispatcherClient) InterruptTurn(ctx context.Context, request *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error) {
	var response *dispatcherpb.InterruptTurnResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.InterruptTurn(ctx, request)
		return err
	})
	return response, err
}

// OpenSession calls the durable, idempotent Session-opening RPC exactly once.
// A RequestTimeoutError indicates that callers must reconcile or retry with
// the same Session ID.
func (c *DispatcherClient) OpenSession(ctx context.Context, request *dispatcherpb.OpenSessionRequest) (*dispatcherpb.OpenSessionResponse, error) {
	var response *dispatcherpb.OpenSessionResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.OpenSession(ctx, request)
		return err
	})
	return response, err
}

// GetSessionOpening fetches one durable opening by its caller-minted UUID.
func (c *DispatcherClient) GetSessionOpening(ctx context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	var response *dispatcherpb.GetSessionOpeningResponse
	err := c.connection(ctx, func(ctx context.Context, client dispatcherpb.SessionDispatcherClient) error {
		var err error
		response, err = client.GetSessionOpening(ctx, request)
		return err
	})
	return response, err
}

var _ Dispatcher = (*DispatcherClient)(nil)
