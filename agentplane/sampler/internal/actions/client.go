// Package actions is the Central Sampler's delegated client for the Action Service.
package actions

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/actionstate"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// ErrDelegationContext marks a call context that already carries authentication metadata.
// A Central Sampler call must have exactly the SVID and the package-built delegation envelope.
var ErrDelegationContext = errors.New("Action Service call context contains authentication metadata")

// Client owns the Central Sampler's authenticated connection to the Action Service.
type Client struct {
	connection *grpc.ClientConn
	service    action_servicepb.ActionServiceClient
	security   *peertls.OpenedSecurity

	closeOnce sync.Once
	closeErr  error
}

// Dial opens a mutual-TLS connection pinned to the Action Service. The local SVID must identify
// the Central Sampler. Unary RPCs use rpcTimeout; streaming waits are bounded only by their caller's context.
func Dial(ctx context.Context, target string, tls peertls.Config, rpcTimeout time.Duration) (*Client, error) {
	if ctx == nil || target == "" || rpcTimeout <= 0 {
		return nil, errors.New("context, target and a positive RPC timeout are required")
	}
	security := peertls.Security{Mode: peertls.MutualTLS, TLS: &tls}
	opened, err := security.Open(ctx)
	if err != nil {
		return nil, fmt.Errorf("open Central Sampler TLS security: %w", err)
	}
	localIdentity, err := opened.LocalIdentity(x509.ExtKeyUsageClientAuth)
	if err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("read Central Sampler SVID: %w", err)
	}
	if localIdentity != peertls.CentralSamplerURI {
		_ = opened.Close()
		return nil, fmt.Errorf("local SVID identity %q is not the Central Sampler", localIdentity)
	}
	credentials, err := opened.ClientCredentials(peertls.ActionsURI)
	if err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("create Action Service credentials: %w", err)
	}
	connection, err := grpc.NewClient(
		target,
		grpc.WithTransportCredentials(credentials),
		grpc.WithChainUnaryInterceptor(withTimeout(rpcTimeout)),
		grpc.WithDisableRetry(),
		observability.GRPCClientOption(),
	)
	if err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("create Action Service connection: %w", err)
	}
	return &Client{
		connection: connection,
		service:    action_servicepb.NewActionServiceClient(connection),
		security:   opened,
	}, nil
}

// Close closes the gRPC connection and the opened SVID source. It is safe to call more than once.
func (c *Client) Close() error {
	if c == nil {
		return nil
	}
	c.closeOnce.Do(func() {
		if c.connection != nil {
			c.closeErr = c.connection.Close()
		}
		if c.security != nil {
			if err := c.security.Close(); c.closeErr == nil {
				c.closeErr = err
			}
		}
	})
	return c.closeErr
}

// Bind fixes the effective Identity and central Session provenance for every RPC made by the returned Session.
func (c *Client) Bind(sessionID string, identity *identitypb.Identity) (*Session, error) {
	if c == nil || c.service == nil {
		return nil, errors.New("Action Service client is required")
	}
	parsedID, err := uuid.Parse(sessionID)
	if err != nil || parsedID.String() != sessionID {
		return nil, errors.New("Session ID must be a canonical UUID")
	}
	if identity == nil {
		return nil, errors.New("Session Identity is required")
	}
	trustedIdentity := proto.Clone(identity).(*identitypb.Identity)
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: trustedIdentity},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: sessionID}},
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		return nil, fmt.Errorf("invalid bound Session delegation: %w", err)
	}
	return &Session{client: c, delegation: encoded}, nil
}

// Session is the Action Service view for one fixed central Session and Identity.
type Session struct {
	client     *Client
	delegation []byte
}

// Catalog reads the Action Groups available to this Session's Identity.
func (s *Session) Catalog(ctx context.Context) ([]*action_servicepb.ActionGroup, error) {
	callCtx, err := s.delegatedContext(ctx)
	if err != nil {
		return nil, err
	}
	response, err := s.client.service.ListActionGroups(callCtx, &action_servicepb.ListActionGroupsRequest{})
	if err != nil {
		return nil, err
	}
	return response.GetActionGroups(), nil
}

// Submit submits a request once. A failed response is returned as-is so callers can recover by the same key.
func (s *Session) Submit(ctx context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.ActionRequest, error) {
	if request == nil {
		return nil, errors.New("Action request is required")
	}
	callCtx, err := s.delegatedContext(ctx)
	if err != nil {
		return nil, err
	}
	response, err := s.client.service.SubmitActionRequest(callCtx, proto.Clone(request).(*action_servicepb.SubmitActionRequestRequest))
	if err != nil {
		return nil, err
	}
	receipt := response.GetActionRequest()
	if receipt == nil {
		return nil, errors.New("Action Service submitted without a receipt")
	}
	if receipt.GetIdempotencyKey() != request.GetIdempotencyKey() {
		return nil, errors.New("Action Service submitted a receipt for a different idempotency key")
	}
	if !canonicalUUID(receipt.GetId()) {
		return nil, errors.New("Action Service submitted a receipt with an invalid request ID")
	}
	return receipt, nil
}

// Recover reads the request recorded under exactly key. It never submits or changes the key.
// A missing receipt returns a gRPC NotFound error.
func (s *Session) Recover(ctx context.Context, key string) (*action_servicepb.ActionRequest, error) {
	if key == "" {
		return nil, errors.New("idempotency key is required")
	}
	callCtx, err := s.delegatedContext(ctx)
	if err != nil {
		return nil, err
	}
	response, err := s.client.service.ListActionRequests(callCtx, &action_servicepb.ListActionRequestsRequest{IdempotencyKey: proto.String(key)})
	if err != nil {
		return nil, err
	}
	switch len(response.GetActionRequests()) {
	case 0:
		return nil, status.Error(codes.NotFound, "Action request was not found under the idempotency key")
	case 1:
		request := response.GetActionRequests()[0]
		if request == nil || request.GetIdempotencyKey() != key {
			return nil, errors.New("Action Service returned a receipt for a different idempotency key")
		}
		if !canonicalUUID(request.GetId()) {
			return nil, errors.New("Action Service returned a receipt with an invalid request ID")
		}
		return request, nil
	default:
		return nil, fmt.Errorf("Action Service returned %d requests for one idempotency key", len(response.GetActionRequests()))
	}
}

// Await follows a request until the Action Service reports a terminal receipt. A dropped stream is
// re-opened with capped backoff; the caller context is the only overall wait bound.
func (s *Session) Await(ctx context.Context, requestID string) (*action_servicepb.ActionRequest, error) {
	if !canonicalUUID(requestID) {
		return nil, errors.New("Action request ID must be a canonical UUID")
	}
	callCtx, err := s.delegatedContext(ctx)
	if err != nil {
		return nil, err
	}
	var last *action_servicepb.ActionRequest
	backoff := retry.WithCappedDuration(30*time.Second,
		retry.WithJitterPercent(50, retry.NewExponential(500*time.Millisecond)))
	err = retry.Do(callCtx, backoff, func(ctx context.Context) error {
		// Every return leaves the stream unread; cancelling releases it instead of holding it for callCtx's lifetime.
		ctx, cancel := context.WithCancel(ctx)
		defer cancel()
		stream, err := s.client.service.WatchActionRequest(ctx, &action_servicepb.WatchActionRequestRequest{
			RequestId: requestID,
			Until:     action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL,
		})
		if err != nil {
			return retryableStreamError(ctx, err)
		}
		for {
			frame, recvErr := stream.Recv()
			if recvErr != nil {
				return retryableStreamError(ctx, recvErr)
			}
			if frame == nil || frame.GetActionRequest() == nil {
				return errors.New("Action Service watch returned an empty receipt")
			}
			last = frame.GetActionRequest()
			if last.GetId() != requestID {
				return errors.New("Action Service watch returned a different request")
			}
			if actionstate.Terminal(last.GetState()) {
				return nil
			}
		}
	})
	return last, err
}

func (s *Session) delegatedContext(ctx context.Context) (context.Context, error) {
	if s == nil || s.client == nil || s.client.service == nil {
		return nil, errors.New("bound Action Service Session is required")
	}
	if ctx == nil {
		return nil, errors.New("call context is required")
	}
	md, _ := metadata.FromOutgoingContext(ctx)
	for key := range md {
		if strings.EqualFold(key, "authorization") || strings.EqualFold(key, auth.DelegationMetadataKey) {
			return nil, ErrDelegationContext
		}
	}
	return metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, string(s.delegation)), nil
}

func retryableStreamError(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if errors.Is(err, io.EOF) {
		return retry.RetryableError(err)
	}
	switch status.Code(err) {
	case codes.Unavailable, codes.DeadlineExceeded, codes.ResourceExhausted, codes.Canceled:
		return retry.RetryableError(err)
	default:
		return err
	}
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed.String() == value
}

func withTimeout(timeout time.Duration) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, request, reply any, connection *grpc.ClientConn, invoke grpc.UnaryInvoker, options ...grpc.CallOption) error {
		ctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		return invoke(ctx, method, request, reply, connection, options...)
	}
}
