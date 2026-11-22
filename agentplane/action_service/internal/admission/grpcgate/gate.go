package grpcgate

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/web_push/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	operatoroidc "github.com/agentydragon/ducktape/agentplane/identity/operator" // The API package has an operator projection helper.
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
)

// Callers says whether a live PolicyBinding names a workload Identity incarnation.
// An implementation logs its refusals.
type Callers interface {
	AdmitsIdentity(identity auth.Identity) bool
}

// ErrDraining is a call that reaches a service that no longer accepts work.
var ErrDraining = errors.New("Action Service is draining")

// ErrUnauthenticated does not disclose whether the presented Identity exists or is admitted.
var ErrUnauthenticated = status.Error(codes.Unauthenticated, "invalid credentials")

// ErrorMapper translates handler failures using the RPC service's domain contract.
type ErrorMapper func(context.Context, error) error

// Options configures admission and the RPC-owned error mapper.
type Options struct {
	ErrorMapper      ErrorMapper
	ServiceReaders   []string
	OperatorServices []string
	// SVID resolves verified workload certificates on the shared TLS listener; operator tokens still use OIDC.
	SVID    *auth.SVIDResolver
	Callers Callers
	// Operators verifies an operator's token; nil admits no operator.
	Operators *operatoroidc.Verifier
	// ConnectionDelegation carries method-scoped authority for the MCP edge. Only the Action
	// methods listed in this map may receive Connection provenance.
	ConnectionDelegation auth.Methods
	// SessionDelegation carries method-scoped authority for the Central Sampler. Only its
	// explicitly listed Action methods may receive Session provenance.
	SessionDelegation auth.Methods
}

// Gate admits only methods its service policy lists. It refuses every call while the service drains, resolves the
// credentials to the principal the call acts as, and answers a failed call with the status code that names its failure.
type Gate struct {
	failure              ErrorMapper
	serviceReaders       sets.Set[string]
	operatorServices     sets.Set[string]
	svid                 *auth.SVIDResolver
	callers              Callers
	operators            *operatoroidc.Verifier
	connectionDelegation auth.Methods
	sessionDelegation    auth.Methods
	methods              map[string]func(context.Context) (Admission, error)
	// draining ends when the service stops accepting work; open watches end with it.
	draining context.Context
	drain    context.CancelFunc
}

func New(options Options) *Gate {
	if options.ErrorMapper == nil {
		panic("grpcgate: ErrorMapper is required")
	}
	draining, drain := context.WithCancel(context.Background())
	g := &Gate{
		failure:        options.ErrorMapper,
		serviceReaders: sets.New(options.ServiceReaders...), operatorServices: sets.New(options.OperatorServices...),
		svid: options.SVID, callers: options.Callers, operators: options.Operators,
		connectionDelegation: options.ConnectionDelegation,
		sessionDelegation:    options.SessionDelegation,
		draining:             draining, drain: drain,
	}
	g.methods = map[string]func(context.Context) (Admission, error){
		action_servicepb.ActionService_SubmitActionRequest_FullMethodName:                         g.authenticateAction(action_servicepb.ActionService_SubmitActionRequest_FullMethodName),
		action_servicepb.ActionService_ListActionRequests_FullMethodName:                          g.authenticateAction(action_servicepb.ActionService_ListActionRequests_FullMethodName),
		action_servicepb.ActionService_ListActionRequestHistory_FullMethodName:                    g.authenticateAction(action_servicepb.ActionService_ListActionRequestHistory_FullMethodName),
		action_servicepb.ActionService_GetActionRequest_FullMethodName:                            g.authenticateAction(action_servicepb.ActionService_GetActionRequest_FullMethodName),
		action_servicepb.ActionService_WatchActionRequest_FullMethodName:                          g.authenticateAction(action_servicepb.ActionService_WatchActionRequest_FullMethodName),
		action_servicepb.ActionService_WatchActionRequests_FullMethodName:                         g.authenticateAction(action_servicepb.ActionService_WatchActionRequests_FullMethodName),
		action_servicepb.ActionService_CancelActionRequest_FullMethodName:                         g.authenticateAction(action_servicepb.ActionService_CancelActionRequest_FullMethodName),
		action_servicepb.ActionService_ListActionRequestEvents_FullMethodName:                     g.authenticateAction(action_servicepb.ActionService_ListActionRequestEvents_FullMethodName),
		action_servicepb.ActionService_ListActionGroups_FullMethodName:                            g.authenticateAction(action_servicepb.ActionService_ListActionGroups_FullMethodName),
		action_servicepb.ActionService_WatchActionGroups_FullMethodName:                           g.authenticateAction(action_servicepb.ActionService_WatchActionGroups_FullMethodName),
		action_servicepb.ActionService_GetAction_FullMethodName:                                   g.authenticateAction(action_servicepb.ActionService_GetAction_FullMethodName),
		action_servicepb.ActionService_GetActionPolicy_FullMethodName:                             g.authenticateAction(action_servicepb.ActionService_GetActionPolicy_FullMethodName),
		action_servicepb.ActionService_WatchActionPolicy_FullMethodName:                           g.authenticateAction(action_servicepb.ActionService_WatchActionPolicy_FullMethodName),
		operator_action_servicepb.OperatorActionService_DecideActionRequest_FullMethodName:        g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_GetGmailResourceSummaries_FullMethodName:  g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_GetGitHubLabelSummaries_FullMethodName:    g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_GetGmailThreadSummaries_FullMethodName:    g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_GetTanaNodeSummaries_FullMethodName:       g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_WatchMcpServers_FullMethodName:            g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_StartMcpServerLinkage_FullMethodName:      g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_DisconnectMcpServer_FullMethodName:        g.authenticateOperator,
		web_push_servicepb.WebPushService_GetPushConfig_FullMethodName:                            g.authenticateOperator,
		web_push_servicepb.WebPushService_ListPushSubscriptions_FullMethodName:                    g.authenticateOperator,
		web_push_servicepb.WebPushService_WatchPushSubscriptions_FullMethodName:                   g.authenticateOperator,
		web_push_servicepb.WebPushService_CreatePushSubscription_FullMethodName:                   g.authenticateOperator,
		web_push_servicepb.WebPushService_DeletePushSubscription_FullMethodName:                   g.authenticateOperator,
		operator_action_servicepb.OperatorActionService_GetGrocyReferenceSummaries_FullMethodName: g.authenticateOperator,
		// The OAuth callback proves its own single-use state; the metadata document is public.
		linkage_servicepb.LinkageService_CompleteCallback_FullMethodName:  nil,
		linkage_servicepb.LinkageService_GetClientMetadata_FullMethodName: nil,
		healthpb.Health_Check_FullMethodName:                              nil,
		healthpb.Health_List_FullMethodName:                               nil,
		healthpb.Health_Watch_FullMethodName:                              nil,

		operator_action_servicepb.OperatorActionService_GetGoogleCalendarEventPreview_FullMethodName: g.authenticateOperator,
	}
	return g
}

// Drain refuses every call from now on and ends the open watches.
func (g *Gate) Drain() { g.drain() }

// Admission is the principal the verified credentials proved.
type Admission struct {
	Principal   store.ReadPrincipal
	Attribution *store.DelegationAttribution
	delegation  delegationProfile
	authContext context.Context
}

// delegationProfile selects the authority and provenance contract for an admitted call.
// The zero value is a direct call, which keeps ordinary admissions safe by default.
type delegationProfile uint8

const (
	directDelegation delegationProfile = iota
	connectionDelegation
	sessionDelegation
)

type admissionKey struct{}

// Admitted is the principal the Gate admitted the call as.
func Admitted(ctx context.Context) Admission { return ctx.Value(admissionKey{}).(Admission) }

// bearer is the call's one Bearer token.
func bearer(ctx context.Context) (string, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	token, err := auth.BearerToken(md.Get("authorization"))
	if err != nil {
		return "", ErrUnauthenticated
	}
	return token, nil
}

// authenticate verifies a workload or service SVID, or a separately configured operator ID token.
func (g *Gate) authenticate(ctx context.Context) (Admission, error) {
	if peertls.HasSVIDPeer(ctx) {
		id, err := peertls.PeerIdentity(ctx)
		if err != nil {
			return Admission{}, ErrUnauthenticated
		}
		if peertls.IsServiceURI(id) {
			md, _ := metadata.FromIncomingContext(ctx)
			if len(md.Get(auth.DelegationMetadataKey)) > 0 {
				return Admission{}, ErrUnauthenticated
			}
			if len(md.Get("authorization")) > 0 {
				return g.authenticateOperator(ctx)
			}
			if !g.serviceReaders.Has(id) {
				return Admission{}, ErrUnauthenticated
			}
			return Admission{Principal: store.ServiceSVIDReaderPrincipal{SPIFFEID: id}}, nil
		}
		md, _ := metadata.FromIncomingContext(ctx)
		if len(md.Get("authorization")) > 0 || len(md.Get(auth.DelegationMetadataKey)) > 0 || g.svid == nil {
			return Admission{}, ErrUnauthenticated
		}
		subject, err := g.svid.ResolveSubject(ctx)
		if err != nil {
			return Admission{}, err
		}
		identity, ok := subject.(auth.IdentityPrincipal)
		if !ok {
			return Admission{}, ErrUnauthenticated
		}
		return g.workloadIdentity(identity.Identity)
	}
	token, err := bearer(ctx)
	if err != nil {
		return Admission{}, err
	}
	if g.operators != nil && g.operators.MatchesUnverifiedIssuer(token) {
		return g.operator(ctx, token)
	}
	return Admission{}, ErrUnauthenticated
}

// workloadIdentity admits an Identity incarnation a live PolicyBinding names, as itself. No Identity is a service
// reader: the reader allowlist names exact service SPIFFE IDs.
func (g *Gate) workloadIdentity(identity auth.Identity) (Admission, error) {
	if !g.callers.AdmitsIdentity(identity) {
		return Admission{}, ErrUnauthenticated
	}
	return Admission{Principal: store.IdentityPrincipal{Identity: store.Identity{Name: identity.Name, UID: identity.UID}}}, nil
}

// DrainingContext is cancelled once the service drains.
func (g *Gate) DrainingContext() context.Context { return g.draining }

// Recheck verifies the call's credentials again before every watch frame: it fails once they are no
// longer accepted as the principal the call was admitted as.
func (g *Gate) Recheck(ctx context.Context) error {
	profile := Admitted(ctx).delegation
	if profile != directDelegation {
		if err := auth.RevalidateStream(ctx); err != nil {
			return err
		}
		verified, err := auth.Admitted(ctx)
		if err != nil {
			return ErrUnauthenticated
		}
		var identityCaller auth.Identity
		switch profile {
		case sessionDelegation:
			identityCaller, err = g.sessionIdentity(verified)
		case connectionDelegation:
			identityCaller, err = g.connectionIdentity(verified)
		default:
			return ErrUnauthenticated
		}
		if err != nil {
			return err
		}

		identity := store.IdentityPrincipal{Identity: store.Identity{Name: identityCaller.Name, UID: identityCaller.UID}}
		if identity != Admitted(ctx).Principal {
			return ErrUnauthenticated
		}
		return nil
	}
	current, err := g.authenticate(ctx)
	if err != nil {
		return err
	}
	if current.Principal != Admitted(ctx).Principal {
		return ErrUnauthenticated
	}
	return nil
}

// authenticateAction accepts Connection delegation only from the existing Connection rules, and
// Session delegation only from the Central Sampler's separate, narrower rules. Direct
// workload/SVID authentication keeps the existing path.
func (g *Gate) authenticateAction(method string) func(context.Context) (Admission, error) {
	return func(ctx context.Context) (Admission, error) {
		md, _ := metadata.FromIncomingContext(ctx)
		delegated := len(md.Get(auth.DelegationMetadataKey)) != 0
		for _, value := range md.Get("authorization") {
			delegated = delegated || strings.HasPrefix(value, auth.DelegatedScheme+" ")
		}
		if !delegated {
			result, err := g.authenticate(ctx)
			if err != nil {
				return Admission{}, err
			}
			if _, service := result.Principal.(store.ServiceSVIDReaderPrincipal); service && !serviceReadMethod(method) {
				return Admission{}, status.Error(codes.PermissionDenied, "service is not admitted on this method")
			}
			return result, nil
		}
		delegation := g.connectionDelegation
		profile := connectionDelegation
		if peertls.HasSVIDPeer(ctx) {
			id, err := peertls.PeerIdentity(ctx)
			if err != nil {
				return Admission{}, ErrUnauthenticated
			}
			if id == peertls.CentralSamplerURI {
				delegation = g.sessionDelegation
				profile = sessionDelegation
			}
		}
		if _, configured := delegation[method]; !configured {
			return Admission{}, ErrUnauthenticated
		}
		admittedContext, err := delegation.AdmitContext(ctx, method, 5*time.Second)
		if err != nil {
			return Admission{}, err
		}
		verified, err := auth.Admitted(admittedContext)
		if err != nil {
			return Admission{}, ErrUnauthenticated
		}
		var effective auth.Identity
		switch profile {
		case sessionDelegation:
			effective, err = g.sessionIdentity(verified)
		case connectionDelegation:
			effective, err = g.connectionIdentity(verified)
		default:
			return Admission{}, ErrUnauthenticated
		}
		if err != nil {
			return Admission{}, err
		}

		attribution, err := recordedDelegation(verified)
		if err != nil {
			return Admission{}, err
		}
		return Admission{
			Attribution: attribution,
			delegation:  profile,
			Principal: store.IdentityPrincipal{Identity: store.Identity{
				Name: effective.Name, UID: effective.UID,
			}},
			authContext: admittedContext,
		}, nil
	}
}

// authenticateOperator resolves the bearer of a call only an operator may make: a workload's token is not
// reviewed.
func (g *Gate) authenticateOperator(ctx context.Context) (Admission, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get(auth.DelegationMetadataKey)) > 0 {
		return Admission{}, ErrUnauthenticated
	}
	if peertls.HasSVIDPeer(ctx) {
		id, err := peertls.PeerIdentity(ctx)
		if err != nil || !g.operatorServices.Has(id) {
			return Admission{}, ErrUnauthenticated
		}
	}
	token, err := bearer(ctx)
	if err != nil {
		return Admission{}, err
	}
	if g.operators == nil || !g.operators.MatchesUnverifiedIssuer(token) {
		return Admission{}, ErrUnauthenticated
	}
	return g.operator(ctx, token)
}

// rejectMixedCredentials applies to public and protected methods alike: a client certificate cannot be paired with
// a bearer or delegated actor to select a different authorization path.
func (g *Gate) rejectMixedCredentials(ctx context.Context) error {
	if !peertls.HasSVIDPeer(ctx) {
		return nil
	}
	md, _ := metadata.FromIncomingContext(ctx)
	id, err := peertls.PeerIdentity(ctx)
	if err != nil {
		return ErrUnauthenticated
	}
	if peertls.IsServiceURI(id) {
		if len(md.Get("authorization")) > 0 && (len(md.Get(auth.DelegationMetadataKey)) > 0 || !g.operatorServices.Has(id)) {
			return ErrUnauthenticated
		}
		return nil
	}
	if len(md.Get("authorization")) > 0 || len(md.Get(auth.DelegationMetadataKey)) > 0 {
		return ErrUnauthenticated
	}
	return nil
}

func (g *Gate) operator(ctx context.Context, token string) (Admission, error) {
	verified, err := g.operators.Verify(ctx, token)
	if err != nil {
		return Admission{}, ErrUnauthenticated
	}
	return Admission{Principal: store.OperatorPrincipal{Issuer: verified.Issuer, Subject: verified.Subject}}, nil
}

// authenticator resolves the method's authentication policy and reports whether the method is admitted at all.
func (g *Gate) authenticator(method string) (func(context.Context) (Admission, error), bool) {
	authenticate, ok := g.methods[method]
	return authenticate, ok
}

// admit is the Gate's work before a gated call runs: the context it runs in, which carries its Principal.
func (g *Gate) admit(ctx context.Context, authenticate func(context.Context) (Admission, error)) (context.Context, error) {
	if g.draining.Err() != nil {
		return nil, g.failure(ctx, ErrDraining)
	}
	admitted, err := authenticate(ctx)
	if err != nil {
		return nil, err
	}
	if admitted.authContext != nil {
		ctx = admitted.authContext
	}
	return context.WithValue(ctx, admissionKey{}, admitted), nil
}

// UnaryInterceptor admits a unary call and answers its failure.
func (g *Gate) UnaryInterceptor(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	if err := g.rejectMixedCredentials(ctx); err != nil {
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
		return nil, g.failure(admittedContext, err)
	}
	return response, nil
}

// StreamInterceptor admits a streaming call and answers its failure.
func (g *Gate) StreamInterceptor(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	if err := g.rejectMixedCredentials(stream.Context()); err != nil {
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
	return g.failure(admittedContext, handler(server, admittedStream{ServerStream: stream, ctx: admittedContext}))
}

// admittedStream is a stream whose context carries the principal the Gate admitted it as.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }

// connectionIdentity authorizes only the effective Identity. The service actor's
// method grant comes from the certificate Rule, never from Identity bindings.
func (g *Gate) connectionIdentity(proof auth.Admission) (auth.Identity, error) {
	var effective auth.Identity
	switch delegated := proof.(type) {
	case auth.DelegatedServiceIdentityAdmission:
		if delegated.Provenance.GetConnection() == nil {
			return effective, ErrUnauthenticated
		}
		effective = delegated.EffectiveCaller
	default:
		return effective, ErrUnauthenticated
	}
	if !g.callers.AdmitsIdentity(effective) {
		return auth.Identity{}, ErrUnauthenticated
	}
	return effective, nil
}

// sessionIdentity accepts only the Central Sampler's typed Session attribution. The service
// actor is independently scoped by sessionDelegation's full-method rules.
func (g *Gate) sessionIdentity(proof auth.Admission) (auth.Identity, error) {
	delegated, ok := proof.(auth.DelegatedServiceIdentityAdmission)
	if !ok || delegated.Actor.SPIFFEID != peertls.CentralSamplerURI || delegated.Provenance.GetSession() == nil {
		return auth.Identity{}, ErrUnauthenticated
	}
	if !g.callers.AdmitsIdentity(delegated.EffectiveCaller) {
		return auth.Identity{}, ErrUnauthenticated
	}
	return delegated.EffectiveCaller, nil
}

func serviceReadMethod(method string) bool {
	switch method {
	case action_servicepb.ActionService_ListActionRequests_FullMethodName,
		action_servicepb.ActionService_ListActionRequestHistory_FullMethodName,
		action_servicepb.ActionService_GetActionRequest_FullMethodName,
		action_servicepb.ActionService_WatchActionRequest_FullMethodName,
		action_servicepb.ActionService_WatchActionRequests_FullMethodName,
		action_servicepb.ActionService_ListActionRequestEvents_FullMethodName,
		action_servicepb.ActionService_ListActionGroups_FullMethodName,
		action_servicepb.ActionService_WatchActionGroups_FullMethodName,
		action_servicepb.ActionService_GetAction_FullMethodName:
		return true
	default:
		return false
	}
}
