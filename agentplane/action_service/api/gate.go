package api

import (
	"context"
	"errors"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// Callers says whether a ServiceAccount may call as a workload: whether it carries the
// `agentplane.allegedly.works/use-action-service` label, in a view of the ServiceAccounts the service can vouch
// for. An implementation logs its refusals.
type Callers interface {
	Admits(account auth.Account) bool
}

// errDraining is a call that reaches a service that no longer accepts work.
var errDraining = errors.New("Action Service is draining")

// unauthenticated is the one answer to a bearer the service does not accept: it does not say why, so a caller
// does not learn from the difference whether an account exists or carries the label.
var unauthenticated = status.Error(codes.Unauthenticated, "invalid bearer")

// The method prefixes of the services the Gate admits callers to; it lets every other call, such as the health
// service's, through. The Connection service is served by the standalone MCP edge but uses this shared verifier.
const (
	actionService     = "/agentplane.action_service.ActionService/"
	operatorService   = "/agentplane.action_service.OperatorActionService/"
	webPushService    = "/agentplane.action_service.WebPushService/"
	connectionService = "/agentplane.mcp_edge.ConnectionService/"
)

// GateOptions are whom a Gate admits.
type GateOptions struct {
	// Workloads reviews a workload's bearer; it admits only the namespaces whose ServiceAccounts may call.
	Workloads *auth.Resolver
	Callers   Callers
	// ReaderAccounts read every account's requests, labeled or not, and change none.
	ReaderAccounts []auth.Account
	// Operators verifies an operator's token; nil admits no operator.
	Operators *OperatorVerifier
}

// Gate admits the calls of the Action Service's own services. It refuses every call while the service drains,
// resolves the bearer to the principal the call acts as, and answers a failed call with the status code that
// names its failure.
type Gate struct {
	workloads *auth.Resolver
	callers   Callers
	readers   map[auth.Account]struct{}
	operators *OperatorVerifier
	// draining ends when the service stops accepting work; open watches end with it.
	draining context.Context
	drain    context.CancelFunc
}

func NewGate(options GateOptions) *Gate {
	readers := make(map[auth.Account]struct{}, len(options.ReaderAccounts))
	for _, account := range options.ReaderAccounts {
		readers[account] = struct{}{}
	}
	draining, drain := context.WithCancel(context.Background())
	return &Gate{
		workloads: options.Workloads, callers: options.Callers, readers: readers, operators: options.Operators,
		draining: draining, drain: drain,
	}
}

// Drain refuses every call from now on and ends the open watches.
func (g *Gate) Drain() { g.drain() }

// admission is the principal a bearer proved.
type admission struct {
	principal store.ReadPrincipal
}

type admissionKey struct{}

// admitted is the principal the Gate admitted the call as.
func admitted(ctx context.Context) admission { return ctx.Value(admissionKey{}).(admission) }

// bearer is the call's one Bearer token.
func bearer(ctx context.Context) (string, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	token, err := auth.BearerToken(md.Get("authorization"))
	if err != nil {
		return "", unauthenticated
	}
	return token, nil
}

// authenticate resolves the call's bearer. A token its unverified issuer claim names as the operators' is an
// operator's, and only its issuer may accept it; any other token is a workload's, which the API server reviews.
// Each path verifies the token in full, so the claim only picks the verifier.
func (g *Gate) authenticate(ctx context.Context) (admission, error) {
	token, err := bearer(ctx)
	if err != nil {
		return admission{}, err
	}
	if g.operators != nil && g.operators.issued(token) {
		return g.operator(ctx, token)
	}
	workload, err := g.workloads.ResolveServiceAccountToken(ctx, token)
	switch {
	case errors.Is(err, auth.ErrInvalidBearer):
		return admission{}, unauthenticated
	case err != nil:
		return admission{}, failure(ctx, err)
	}
	account := action.ServiceAccount{Namespace: workload.Account.Namespace, Name: workload.Account.Name}
	if _, reader := g.readers[workload.Account]; reader {
		return admission{principal: store.ServiceReaderPrincipal{Account: account}}, nil
	}
	if !g.callers.Admits(workload.Account) {
		return admission{}, unauthenticated
	}
	return admission{principal: store.CallerPrincipal{Account: account}}, nil
}

// Operator is the issuer and subject of the operator the Gate admitted a call of an operator-only service as.
func (g *Gate) Operator(ctx context.Context) (issuer, subject string) {
	operator := admitted(ctx).principal.(store.OperatorPrincipal)
	return operator.Issuer, operator.Subject
}

// Draining is closed once the service drains.
func (g *Gate) Draining() <-chan struct{} { return g.draining.Done() }

// Recheck verifies the call's bearer again, as a watch does before every frame: it fails once the bearer is no
// longer accepted as the principal the call was admitted as.
func (g *Gate) Recheck(ctx context.Context) error {
	current, err := g.authenticate(ctx)
	if err != nil {
		return err
	}
	if current.principal != admitted(ctx).principal {
		return unauthenticated
	}
	return nil
}

// authenticateOperator resolves the bearer of a call only an operator may make: a workload's token is not
// reviewed.
func (g *Gate) authenticateOperator(ctx context.Context) (admission, error) {
	token, err := bearer(ctx)
	if err != nil {
		return admission{}, err
	}
	if g.operators == nil || !g.operators.issued(token) {
		return admission{}, unauthenticated
	}
	return g.operator(ctx, token)
}

func (g *Gate) operator(ctx context.Context, token string) (admission, error) {
	operator, err := g.operators.Verify(ctx, token)
	if err != nil {
		return admission{}, unauthenticated
	}
	return admission{principal: operator}, nil
}

// authenticator is how the Gate resolves the bearer of a call to the method, or nil for a call it lets through.
func (g *Gate) authenticator(method string) func(context.Context) (admission, error) {
	switch {
	case strings.HasPrefix(method, actionService):
		return g.authenticate
	case strings.HasPrefix(method, operatorService), strings.HasPrefix(method, webPushService),
		strings.HasPrefix(method, connectionService):
		return g.authenticateOperator
	default:
		return nil
	}
}

// admit is the Gate's work before a gated call runs: the context it runs in, which carries its principal.
func (g *Gate) admit(ctx context.Context, authenticate func(context.Context) (admission, error)) (context.Context, error) {
	if g.draining.Err() != nil {
		return nil, failure(ctx, errDraining)
	}
	admitted, err := authenticate(ctx)
	if err != nil {
		return nil, err
	}
	return context.WithValue(ctx, admissionKey{}, admitted), nil
}

// UnaryInterceptor admits a unary call and answers its failure.
func (g *Gate) UnaryInterceptor(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	authenticate := g.authenticator(info.FullMethod)
	if authenticate == nil {
		return handler(ctx, request)
	}
	admittedContext, err := g.admit(ctx, authenticate)
	if err != nil {
		return nil, err
	}
	response, err := handler(admittedContext, request)
	if err != nil {
		return nil, failure(admittedContext, err)
	}
	return response, nil
}

// StreamInterceptor admits a streaming call and answers its failure.
func (g *Gate) StreamInterceptor(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	authenticate := g.authenticator(info.FullMethod)
	if authenticate == nil {
		return handler(server, stream)
	}
	admittedContext, err := g.admit(stream.Context(), authenticate)
	if err != nil {
		return err
	}
	return failure(admittedContext, handler(server, admittedStream{ServerStream: stream, ctx: admittedContext}))
}

// admittedStream is a stream whose context carries the principal the Gate admitted it as.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }
