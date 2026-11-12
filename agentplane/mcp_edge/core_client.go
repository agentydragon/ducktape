package main

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	mcpauth "github.com/modelcontextprotocol/go-sdk/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type edgeBearerKey struct{}

// callerIdentity is the OAuth grant and exact Identity incarnation an external MCP request is authorized to use.
type callerIdentity struct {
	identity     auth.Identity
	connectionID string
	grantID      string
}

type connectionBearerVerifier func(context.Context, string) (*connections.ResolvedGrant, error)

// coreCaller crosses the edge-to-Actions authority boundary. Every RPC resolves the bearer through its current
// Connection and grant, then delegates that Identity using the edge's own projected Action-audience token. An open
// stream rechecks the grant and binding on a ticker.
type coreCaller struct {
	authority     connectionBearerVerifier
	recheckPeriod time.Duration
	logger        *slog.Logger
}

func (c *coreCaller) identity(ctx context.Context) (callerIdentity, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	bearer, _ := ctx.Value(edgeBearerKey{}).(string)
	if bearer == "" {
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	}
	grant, err := c.authority(ctx, bearer)
	switch {
	case errors.Is(err, mcpauth.ErrInvalidToken):
		return callerIdentity{}, status.Error(codes.Unauthenticated, "invalid bearer")
	case err != nil:
		if c.logger != nil {
			c.logger.WarnContext(ctx, "resolving MCP Connection authority failed", "error", err)
		}
		return callerIdentity{}, status.Error(codes.Unavailable, "Connection authority unavailable")
	}
	if grant == nil || grant.ConnectionID == [16]byte{} || grant.GrantID == [16]byte{} || grant.Identity.Name == "" || grant.Identity.UID == "" {
		return callerIdentity{}, status.Error(codes.Internal, "invalid Connection authority")
	}
	return callerIdentity{
		identity: grant.Identity, connectionID: grant.ConnectionID.String(), grantID: grant.GrantID.String(),
	}, nil
}

func (c *coreCaller) delegated(ctx context.Context) (context.Context, callerIdentity, error) {
	identity, err := c.identity(ctx)
	if err != nil {
		return nil, callerIdentity{}, err
	}
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{
			Name: identity.identity.Name, Uid: identity.identity.UID,
		}},
		Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{
			ConnectionId: identity.connectionID, GrantId: identity.grantID,
		}},
	}
	delegated, err := workloadgrpc.WithDelegation(ctx, envelope)
	if err != nil {
		return nil, callerIdentity{}, status.Error(codes.Internal, "could not construct Connection delegation")
	}
	return delegated, identity, nil
}

func (c *coreCaller) recheck(ctx context.Context, original callerIdentity) error {
	current, err := c.identity(ctx)
	if err != nil {
		return err
	}
	if current != original {
		return status.Error(codes.Unauthenticated, "invalid bearer")
	}
	return nil
}

func (c *coreCaller) unary(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	delegated, _, err := c.delegated(ctx)
	if err != nil {
		return err
	}
	return invoke(delegated, method, request, reply, connection, options...)
}

func (c *coreCaller) stream(ctx context.Context, description *grpc.StreamDesc, connection *grpc.ClientConn, method string, start grpc.Streamer, options ...grpc.CallOption) (grpc.ClientStream, error) {
	delegated, identity, err := c.delegated(ctx)
	if err != nil {
		return nil, err
	}
	watchCtx, cancel := context.WithCancel(delegated)
	stream, err := start(watchCtx, description, connection, method, options...)
	if err != nil {
		cancel()
		return nil, err
	}
	checked := &checkedStream{ClientStream: stream, caller: c, ctx: watchCtx, cancel: cancel, identity: identity}
	go checked.watch()
	return checked, nil
}

// checkedStream fails an open watch within one recheck period of grant revocation, Connection rebind, or Identity
// replacement. Frames are not rechecked individually; the receiving Action service also checks the live Identity UID.
type checkedStream struct {
	grpc.ClientStream
	caller   *coreCaller
	ctx      context.Context
	cancel   context.CancelFunc
	identity callerIdentity
	mu       sync.Mutex
	err      error
}

func (s *checkedStream) watch() {
	period := s.caller.recheckPeriod
	if period <= 0 {
		period = 5 * time.Second
	}
	ticker := time.NewTicker(period)
	defer ticker.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case <-ticker.C:
			if err := s.caller.recheck(s.ctx, s.identity); err != nil {
				s.mu.Lock()
				s.err = err
				s.mu.Unlock()
				s.cancel()
				return
			}
		}
	}
}

func (s *checkedStream) RecvMsg(message any) error {
	err := s.ClientStream.RecvMsg(message)
	s.mu.Lock()
	revoked := s.err
	s.mu.Unlock()
	if revoked != nil {
		return revoked
	}
	if err != nil {
		s.cancel()
		return err
	}
	return nil
}

func (s *checkedStream) SendMsg(message any) error {
	err := s.ClientStream.SendMsg(message)
	if err != nil {
		s.cancel()
	}
	return err
}

func (s *checkedStream) CloseSend() error {
	err := s.ClientStream.CloseSend()
	if err != nil {
		s.cancel()
	}
	return err
}

func dialActions(target, tokenFile string, timeout time.Duration, tls *environment_runtime_transport.ClientTLS, caller *coreCaller) (*grpc.ClientConn, error) {
	return workloadgrpc.DialDelegated(target, tokenFile, timeout, tls, workloadgrpc.ClientInterceptors{
		Unary: caller.unary, Stream: caller.stream,
	})
}
