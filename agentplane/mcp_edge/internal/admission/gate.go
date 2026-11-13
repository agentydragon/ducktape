package admission

import (
	"context"
	"errors"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

var (
	unauthenticated = status.Error(codes.Unauthenticated, "invalid bearer")
	errDraining     = errors.New("Action Service is draining")
)

// Gate admits operator requests to the MCP edge's Connection API.
type Gate struct {
	operators *operator.Verifier
	methods   map[string]bool
	draining  context.Context
	drain     context.CancelFunc
}

func NewGate(operators *operator.Verifier) *Gate {
	draining, drain := context.WithCancel(context.Background())
	return &Gate{
		operators: operators,
		methods: map[string]bool{
			connection_servicepb.ConnectionService_ListConnectionIdentities_FullMethodName:    true,
			connection_servicepb.ConnectionService_ListConnections_FullMethodName:             true,
			connection_servicepb.ConnectionService_GetConnection_FullMethodName:               true,
			connection_servicepb.ConnectionService_UpdateConnection_FullMethodName:            true,
			connection_servicepb.ConnectionService_RebindConnection_FullMethodName:            true,
			connection_servicepb.ConnectionService_UnbindConnection_FullMethodName:            true,
			connection_servicepb.ConnectionService_WatchConnections_FullMethodName:            true,
			connection_servicepb.ConnectionService_PreviewConnectionEnrollment_FullMethodName: true,
			connection_servicepb.ConnectionService_DecideConnectionEnrollment_FullMethodName:  true,
		},
		draining: draining,
		drain:    drain,
	}
}

type subjectKey struct{}

func subject(ctx context.Context) operator.Subject {
	return ctx.Value(subjectKey{}).(operator.Subject)
}

// Operator is the issuer and subject the Gate admitted the call as.
func (g *Gate) Operator(ctx context.Context) (issuer, operatorSubject string) {
	principal := subject(ctx)
	return principal.Issuer, principal.Subject
}

// DrainingContext is cancelled once the edge starts shutting down.
func (g *Gate) DrainingContext() context.Context { return g.draining }

// Drain refuses new Connection calls and lets existing watches end.
func (g *Gate) Drain() { g.drain() }

// Recheck verifies the bearer again and requires it to prove the same operator admitted at the call boundary.
func (g *Gate) Recheck(ctx context.Context) error {
	current, err := g.authenticate(ctx)
	if err != nil {
		return err
	}
	if current != subject(ctx) {
		return unauthenticated
	}
	return nil
}

func (g *Gate) authenticate(ctx context.Context) (operator.Subject, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if hasSVIDPeer(ctx) || len(md.Get(auth.DelegationMetadataKey)) > 0 {
		return operator.Subject{}, unauthenticated
	}
	token, err := auth.BearerToken(md.Get("authorization"))
	if err != nil || g.operators == nil || !g.operators.MatchesUnverifiedIssuer(token) {
		return operator.Subject{}, unauthenticated
	}
	principal, err := g.operators.Verify(ctx, token)
	if err != nil {
		return operator.Subject{}, unauthenticated
	}
	return principal, nil
}

func hasSVIDPeer(ctx context.Context) bool {
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return false
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	return ok && len(info.State.PeerCertificates) > 0
}

// rejectMixedCredentials applies to health and Connection methods alike: a client certificate cannot be paired with
// a bearer or delegated actor to select a second authorization path.
func rejectMixedCredentials(ctx context.Context) error {
	if !hasSVIDPeer(ctx) {
		return nil
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("authorization")) > 0 || len(md.Get(auth.DelegationMetadataKey)) > 0 {
		return unauthenticated
	}
	return nil
}

func (g *Gate) authenticateMethod(method string) (bool, bool) {
	if g.methods[method] {
		return true, true
	}
	switch method {
	case healthpb.Health_Check_FullMethodName, healthpb.Health_List_FullMethodName, healthpb.Health_Watch_FullMethodName:
		return true, false
	default:
		return false, false
	}
}

func (g *Gate) admit(ctx context.Context) (context.Context, error) {
	if g.draining.Err() != nil {
		return nil, failure(ctx, errDraining)
	}
	principal, err := g.authenticate(ctx)
	if err != nil {
		return nil, err
	}
	return context.WithValue(ctx, subjectKey{}, principal), nil
}

// UnaryInterceptor admits only the nine operator Connection methods and leaves gRPC health to its own handler.
func (g *Gate) UnaryInterceptor(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	if err := rejectMixedCredentials(ctx); err != nil {
		return nil, err
	}
	listed, protected := g.authenticateMethod(info.FullMethod)
	if !listed {
		return nil, status.Error(codes.PermissionDenied, "method not allowed")
	}
	if !protected {
		return handler(ctx, request)
	}
	admittedContext, err := g.admit(ctx)
	if err != nil {
		return nil, err
	}
	response, err := handler(admittedContext, request)
	if err != nil {
		return nil, failure(admittedContext, err)
	}
	return response, nil
}

// StreamInterceptor admits only the operator Connection watch and leaves gRPC health to its own handler.
func (g *Gate) StreamInterceptor(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	if err := rejectMixedCredentials(stream.Context()); err != nil {
		return err
	}
	listed, protected := g.authenticateMethod(info.FullMethod)
	if !listed {
		return status.Error(codes.PermissionDenied, "method not allowed")
	}
	if !protected {
		return handler(server, stream)
	}
	admittedContext, err := g.admit(stream.Context())
	if err != nil {
		return err
	}
	return failure(admittedContext, handler(server, admittedStream{ServerStream: stream, ctx: admittedContext}))
}

type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }

// failure preserves the established status for drain, handler and context failures.
func failure(ctx context.Context, err error) error {
	switch {
	case err == nil:
		return nil
	case status.Code(err) != codes.Unknown:
		return err
	case errors.Is(err, errDraining):
		return status.Error(codes.Unavailable, errDraining.Error())
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "action request deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	default:
		slog.WarnContext(ctx, "action request failed", "error", err)
		return status.Error(codes.Unavailable, "the Action Service is temporarily unavailable")
	}
}
