package api

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
)

// Callers says whether a workload may call: for a ServiceAccount, whether it carries the Action Service caller
// label in a view of the ServiceAccounts the service can vouch for; for an Identity incarnation, whether a live
// PolicyBinding names it. An implementation logs its refusals.
type Callers interface {
	Admits(account auth.Account) bool
	AdmitsIdentity(identity auth.Identity) bool
}

// errDraining is a call that reaches a service that no longer accepts work.
var errDraining = errors.New("Action Service is draining")

// unauthenticated is the one answer to a bearer the service does not accept: it does not say why, so a caller
// does not learn from the difference whether an account exists or carries the label.
var unauthenticated = status.Error(codes.Unauthenticated, "invalid bearer")

// GateOptions are whom a Gate admits.
type GateOptions struct {
	// Workloads reviews a workload's bearer; it admits only the namespaces whose ServiceAccounts may call.
	Workloads *auth.Resolver
	// SVID resolves verified workload certificates on the shared TLS listener; operator tokens still use OIDC.
	SVID    *auth.SVIDResolver
	Callers Callers
	// ReaderAccounts read every account's requests, labeled or not, and change none.
	ReaderAccounts []auth.Account
	// Operators verifies an operator's token; nil admits no operator.
	Operators *OperatorVerifier
	// AdditionalOperatorMethods names methods owned by another service that this Gate serves.
	AdditionalOperatorMethods []string
}

// Gate admits only methods its service policy lists. It refuses every call while the service drains, resolves the
// bearer to the principal the call acts as, and answers a failed call with the status code that names its failure.
type Gate struct {
	workloads *auth.Resolver
	svid      *auth.SVIDResolver
	callers   Callers
	readers   sets.Set[auth.Account]
	operators *OperatorVerifier
	methods   map[string]func(context.Context) (admission, error)
	// draining ends when the service stops accepting work; open watches end with it.
	draining context.Context
	drain    context.CancelFunc
}

func NewGate(options GateOptions) *Gate {
	draining, drain := context.WithCancel(context.Background())
	g := &Gate{
		workloads: options.Workloads, svid: options.SVID, callers: options.Callers, readers: sets.New(options.ReaderAccounts...), operators: options.Operators,
		draining: draining, drain: drain,
	}
	g.methods = map[string]func(context.Context) (admission, error){
		action_servicepb.ActionService_SubmitActionRequest_FullMethodName:                    g.authenticate,
		action_servicepb.ActionService_ListActionRequests_FullMethodName:                     g.authenticate,
		action_servicepb.ActionService_ListActionRequestHistory_FullMethodName:               g.authenticate,
		action_servicepb.ActionService_GetActionRequest_FullMethodName:                       g.authenticate,
		action_servicepb.ActionService_WatchActionRequest_FullMethodName:                     g.authenticate,
		action_servicepb.ActionService_WatchActionRequests_FullMethodName:                    g.authenticate,
		action_servicepb.ActionService_CancelActionRequest_FullMethodName:                    g.authenticate,
		action_servicepb.ActionService_ListActionRequestEvents_FullMethodName:                g.authenticate,
		action_servicepb.ActionService_ListActionGroups_FullMethodName:                       g.authenticate,
		action_servicepb.ActionService_WatchActionGroups_FullMethodName:                      g.authenticate,
		action_servicepb.ActionService_GetAction_FullMethodName:                              g.authenticate,
		action_servicepb.ActionService_GetActionPolicy_FullMethodName:                        g.authenticate,
		action_servicepb.ActionService_WatchActionPolicy_FullMethodName:                      g.authenticate,
		operator_action_servicepb.OperatorActionService_DecideActionRequest_FullMethodName:   g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_WatchMcpServers_FullMethodName:       g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_StartMcpServerLinkage_FullMethodName: g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_DisconnectMcpServer_FullMethodName:   g.authenticateOperator,
		web_push_servicepb.WebPushService_GetPushConfig_FullMethodName:                       g.authenticateOperator,
		web_push_servicepb.WebPushService_ListPushSubscriptions_FullMethodName:               g.authenticateOperator,
		web_push_servicepb.WebPushService_WatchPushSubscriptions_FullMethodName:              g.authenticateOperator,
		web_push_servicepb.WebPushService_CreatePushSubscription_FullMethodName:              g.authenticateOperator,
		web_push_servicepb.WebPushService_DeletePushSubscription_FullMethodName:              g.authenticateOperator,
		// The OAuth callback proves its own single-use state; the metadata document is public.
		linkage_servicepb.LinkageService_CompleteCallback_FullMethodName:  nil,
		linkage_servicepb.LinkageService_GetClientMetadata_FullMethodName: nil,
		healthpb.Health_Check_FullMethodName:                              nil,
		healthpb.Health_List_FullMethodName:                               nil,
		healthpb.Health_Watch_FullMethodName:                              nil,
	}
	for _, method := range options.AdditionalOperatorMethods {
		g.methods[method] = g.authenticateOperator
	}
	return g
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
	if hasSVIDPeer(ctx) {
		md, _ := metadata.FromIncomingContext(ctx)
		if len(md.Get("authorization")) > 0 || len(md.Get(auth.DelegationMetadataKey)) > 0 || g.svid == nil {
			return admission{}, unauthenticated
		}
		subject, err := g.svid.ResolveSubject(ctx)
		if err != nil {
			return admission{}, err
		}
		switch subject := subject.(type) {
		case auth.Principal:
			return g.workloadAccount(subject.Account, subject.ServiceAccountUID)
		case auth.IdentityPrincipal:
			return g.workloadIdentity(subject.Identity)
		default:
			panic(fmt.Sprintf("unknown workload subject %T", subject))
		}
	}
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
	identityAccount := workload.GetAccount()
	if identityAccount == nil || identityAccount.GetNamespace() == "" || identityAccount.GetName() == "" || workload.GetUid() == "" {
		return admission{}, unauthenticated
	}
	return g.workloadAccount(auth.Account{Namespace: identityAccount.GetNamespace(), Name: identityAccount.GetName()}, workload.GetUid())
}

// workloadAccount retains the existing caller-label and reader authorization for
// both authentication boundaries, including canonical ServiceAccount ownership.
func (g *Gate) workloadAccount(accountRef auth.Account, uid string) (admission, error) {
	account := store.ServiceAccount{Namespace: accountRef.Namespace, Name: accountRef.Name}
	if g.readers.Has(accountRef) {
		return admission{principal: store.ServiceReaderPrincipal{Account: account}}, nil
	}
	if !g.callers.Admits(accountRef) {
		return admission{}, unauthenticated
	}
	return admission{principal: store.CallerPrincipal{
		Account: account, ServiceAccountUID: uid,
	}}, nil
}

// workloadIdentity admits an Identity incarnation a live PolicyBinding names, as itself. No Identity is a service
// reader: the reader allowlist names ServiceAccounts.
func (g *Gate) workloadIdentity(identity auth.Identity) (admission, error) {
	if !g.callers.AdmitsIdentity(identity) {
		return admission{}, unauthenticated
	}
	return admission{principal: store.IdentityPrincipal{Identity: store.Identity{Name: identity.Name, UID: identity.UID}}}, nil
}

// Operator is the issuer and subject of the operator the Gate admitted a call of an operator-only service as.
func (g *Gate) Operator(ctx context.Context) (issuer, subject string) {
	operator := admitted(ctx).principal.(store.OperatorPrincipal)
	return operator.Issuer, operator.Subject
}

// DrainingContext is cancelled once the service drains.
func (g *Gate) DrainingContext() context.Context { return g.draining }

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
	md, _ := metadata.FromIncomingContext(ctx)
	if hasSVIDPeer(ctx) || len(md.Get(auth.DelegationMetadataKey)) > 0 {
		return admission{}, unauthenticated
	}
	token, err := bearer(ctx)
	if err != nil {
		return admission{}, err
	}
	if g.operators == nil || !g.operators.issued(token) {
		return admission{}, unauthenticated
	}
	return g.operator(ctx, token)
}

func hasSVIDPeer(ctx context.Context) bool {
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return false
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	return ok && len(info.State.PeerCertificates) > 0
}

// rejectMixedCredentials applies to public and protected methods alike: a client certificate cannot be paired with
// a bearer or delegated actor to select a different authorization path.
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

func (g *Gate) operator(ctx context.Context, token string) (admission, error) {
	operator, err := g.operators.Verify(ctx, token)
	if err != nil {
		return admission{}, unauthenticated
	}
	return admission{principal: operator}, nil
}

// authenticator resolves the method's bearer policy and reports whether the method is admitted at all.
func (g *Gate) authenticator(method string) (func(context.Context) (admission, error), bool) {
	authenticate, ok := g.methods[method]
	return authenticate, ok
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
	if err := rejectMixedCredentials(ctx); err != nil {
		return nil, err
	}
	authenticate, ok := g.authenticator(info.FullMethod)
	if !ok {
		return nil, status.Error(codes.PermissionDenied, "method not allowed")
	}
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
	if err := rejectMixedCredentials(stream.Context()); err != nil {
		return err
	}
	authenticate, ok := g.authenticator(info.FullMethod)
	if !ok {
		return status.Error(codes.PermissionDenied, "method not allowed")
	}
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
