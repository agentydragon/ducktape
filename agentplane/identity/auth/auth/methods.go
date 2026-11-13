package auth

import (
	"context"
	"fmt"
	"maps"
	"net/http"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/apimachinery/pkg/util/sets"
)

type ruleKind int

const (
	// The zero Rule refuses every caller, so a Rule left unset fails closed.
	ruleRefuse ruleKind = iota
	rulePublic
	ruleAccounts
	ruleSVIDOrAccounts
	ruleReviewedServiceAccount
	ruleReviewedServiceAccountOrIdentity
	ruleAnySubject
)

// Rule is who may call one gRPC method.
type Rule struct {
	kind            ruleKind
	resolver        *Resolver
	subjectResolver SubjectResolver
	accounts        sets.Set[Account]
	delegation      *delegationPolicy
}

// Public admits a call without reading its bearer. Only the standard health service is public: Kubernetes
// probes carry no bearer.
func Public() Rule { return Rule{kind: rulePublic} }

// Accounts admits a caller whose bearer the resolver reviews for one of the listed accounts. An empty list admits nobody.
func Accounts(resolver *Resolver, accounts []Account) Rule {
	return Rule{kind: ruleAccounts, resolver: resolver, accounts: sets.New(accounts...)}
}

// AnySubject admits any workload this resolver authenticates, a ServiceAccount or an Identity
// subject. A ServiceAccount caller is admitted with a Principal; an Identity caller only as an
// IdentityAdmission, so the handler must read Admitted. Only a TokenReview Resolver also supports
// delegated calls.
func AnySubject(subjectResolver SubjectResolver) Rule {
	resolver, isTokenReview := subjectResolver.(*Resolver)
	if isTokenReview && resolver == nil {
		subjectResolver = nil
	}
	return Rule{kind: ruleAnySubject, resolver: resolver, subjectResolver: subjectResolver}
}

// SVIDOrAccounts admits a verified Sandbox SVID, or a bearer for one of the
// listed accounts. The resolver must be bound to the listener's allowed modes. An SVID naming an
// Identity is admitted as AnySubject admits it, so the handler must read Admitted.
func SVIDOrAccounts(resolver *Resolver, accounts []Account) Rule {
	rule := Accounts(resolver, accounts)
	rule.kind = ruleSVIDOrAccounts
	return rule
}

// ReviewedServiceAccount admits any reviewed ServiceAccount in the Resolver's namespace policy.
// It stores a typed ReviewedCaller proof so account-scoped handlers can use the SA UID while
// existing Principal callers continue to require Pod-bound TokenReview claims.
func ReviewedServiceAccount(resolver *Resolver) Rule {
	return Rule{kind: ruleReviewedServiceAccount, resolver: resolver}
}

// ReviewedServiceAccountOrIdentity admits a ServiceAccount exactly as ReviewedServiceAccount does,
// and a workload SVID naming an Identity as an IdentityAdmission, which handlers read with Admitted:
// AdmittedReviewedCaller, Caller and EffectiveCaller refuse it. The resolver's SVID resolver decides
// whether Identity subjects resolve at all.
func ReviewedServiceAccountOrIdentity(resolver *Resolver) Rule {
	return Rule{kind: ruleReviewedServiceAccountOrIdentity, resolver: resolver}
}

// Methods maps the full name (`/package.Service/Method`) of each method a gRPC server serves to the Rule
// admitting its callers. A method it does not list is refused before its bearer is read, so a new RPC is closed
// until its Rule is written.
type Methods map[string]Rule

// WithDelegation returns a copy of m that admits delegation only for the named full RPC methods.
// Every method must already have an authenticated direct Rule; delegations preserve that direct mode.
// identities is nil for a receiver that resolves no Identity (see AllowDelegationFrom).
func (m Methods) WithDelegation(
	methodNames []string,
	delegators []Delegator,
	serviceAccounts ServiceAccountUIDReader,
	identities IdentityUIDReader,
) (Methods, error) {
	methods := make(Methods, len(m))
	maps.Copy(methods, m)
	seen := make(map[string]struct{}, len(methodNames))
	for _, method := range methodNames {
		if _, duplicate := seen[method]; duplicate {
			return nil, fmt.Errorf("delegated method %q is repeated", method)
		}
		seen[method] = struct{}{}
		rule, ok := methods[method]
		if !ok {
			return nil, fmt.Errorf("delegated method %q is not served", method)
		}
		rule, err := rule.AllowDelegationFrom(delegators, serviceAccounts, identities)
		if err != nil {
			return nil, fmt.Errorf("delegated method %q: %w", method, err)
		}
		methods[method] = rule
	}
	return methods, nil
}

// HealthMethods lists the standard health service's methods as Public.
func HealthMethods() Methods {
	return Methods{
		healthpb.Health_Check_FullMethodName: Public(),
		healthpb.Health_List_FullMethodName:  Public(),
		healthpb.Health_Watch_FullMethodName: Public(),
	}
}

var (
	// errMethodNotAllowed refuses a method the server's Methods do not list.
	errMethodNotAllowed = status.Error(codes.PermissionDenied, "method not allowed")
	// errNotAdmitted is a handler asking for the caller of a call no account Rule admitted.
	errNotAdmitted = status.Error(codes.Unauthenticated, "call not admitted")
)

type callerKey struct{}

// WithCaller is the context a call admitted as principal runs in.
func WithCaller(ctx context.Context, principal Principal) context.Context {
	return context.WithValue(ctx, callerKey{}, principal)
}

// Caller is the ServiceAccount principal an account rule admitted. A call no such Rule admitted is
// refused as unauthenticated; Identity callers are read with Admitted.
func Caller(ctx context.Context) (Principal, error) {
	principal, ok := ctx.Value(callerKey{}).(Principal)
	if !ok {
		return Principal{}, errNotAdmitted
	}
	return principal, nil
}

type reviewedCallerKey struct{}

type revalidateKey struct{}

// RevalidateStream reviews the credentials originally admitted for this stream again.
// A successful review must still prove the same direct or delegated identity.
func RevalidateStream(ctx context.Context) error {
	revalidate, ok := ctx.Value(revalidateKey{}).(func(context.Context) error)
	if !ok {
		return errNotAdmitted
	}
	return revalidate(ctx)
}

func sameAdmission(a, b Admission) bool {
	switch first := a.(type) {
	case DirectAdmission:
		second, ok := b.(DirectAdmission)
		if !ok || !sameServiceAccountIdentity(first.Caller, second.Caller) || (first.Pod == nil) != (second.Pod == nil) {
			return false
		}
		return first.Pod == nil || *first.Pod == *second.Pod
	case DelegatedAdmission:
		second, ok := b.(DelegatedAdmission)
		return ok && first.Actor == second.Actor && sameServiceAccountIdentity(first.EffectiveCaller, second.EffectiveCaller) &&
			proto.Equal(first.Provenance, second.Provenance)
	case DelegatedIdentityAdmission:
		second, ok := b.(DelegatedIdentityAdmission)
		return ok && first.Actor == second.Actor && first.EffectiveCaller == second.EffectiveCaller &&
			proto.Equal(first.Provenance, second.Provenance)
	case IdentityAdmission:
		second, ok := b.(IdentityAdmission)
		return ok && first == second
	default:
		return false
	}
}

// WithReviewedCaller stores the account proof and its separate optional Pod proof.
func WithReviewedCaller(ctx context.Context, caller ReviewedCaller) context.Context {
	return context.WithValue(ctx, reviewedCallerKey{}, caller)
}

// AdmittedReviewedCaller is available only to a ServiceAccount caller a ReviewedServiceAccount or
// ReviewedServiceAccountOrIdentity method admitted.
func AdmittedReviewedCaller(ctx context.Context) (ReviewedCaller, error) {
	caller, ok := ctx.Value(reviewedCallerKey{}).(ReviewedCaller)
	if !ok {
		return ReviewedCaller{}, errNotAdmitted
	}
	return caller, nil
}

// ServerOptions admits each call by its method's Rule before any handler runs. admissionTimeout bounds caller
// resolution; zero leaves it bounded by the call's own deadline.
func (m Methods) ServerOptions(admissionTimeout time.Duration) []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.ChainUnaryInterceptor(m.UnaryInterceptor(admissionTimeout)),
		grpc.ChainStreamInterceptor(m.StreamInterceptor(admissionTimeout)),
	}
}

// UnaryInterceptor applies the same method admission to native and in-process RPCs.
func (m Methods) UnaryInterceptor(admissionTimeout time.Duration) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		admitted, err := m.admit(ctx, info.FullMethod, admissionTimeout)
		if err != nil {
			return nil, err
		}
		return handler(admitted, request)
	}
}

// StreamInterceptor admits streams and retains their caller revalidation boundary.
func (m Methods) StreamInterceptor(admissionTimeout time.Duration) grpc.StreamServerInterceptor {
	return func(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		admitted, err := m.admit(stream.Context(), info.FullMethod, admissionTimeout)
		if err != nil {
			return err
		}
		if original, err := Admitted(admitted); err == nil {
			revalidate := func(ctx context.Context) error {
				current, err := m.admit(ctx, info.FullMethod, admissionTimeout)
				if err != nil {
					return err
				}
				identity, err := Admitted(current)
				if err != nil || !sameAdmission(original, identity) {
					return status.Error(codes.Unauthenticated, "stream caller changed")
				}
				return nil
			}
			admitted = context.WithValue(admitted, revalidateKey{}, revalidate)
		}
		return handler(server, admittedStream{ServerStream: stream, ctx: admitted})
	}
}

// admit is the context the call runs in once its Rule admits it.
func (m Methods) admit(ctx context.Context, method string, admissionTimeout time.Duration) (context.Context, error) {
	rule := m[method]
	switch rule.kind {
	case rulePublic:
		md, _ := metadata.FromIncomingContext(ctx)
		if len(md.Get(DelegationMetadataKey)) > 0 || delegatedAttempt(md.Get("authorization")) {
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		}
		return ctx, nil
	case ruleAccounts, ruleSVIDOrAccounts, ruleReviewedServiceAccount, ruleReviewedServiceAccountOrIdentity, ruleAnySubject:
	default:
		return nil, errMethodNotAllowed
	}
	if rule.kind == ruleAnySubject && rule.subjectResolver == nil {
		return nil, errMethodNotAllowed
	}
	resolveContext := ctx
	if admissionTimeout > 0 {
		var cancel context.CancelFunc
		resolveContext, cancel = context.WithTimeout(ctx, admissionTimeout)
		defer cancel()
	}
	md, _ := metadata.FromIncomingContext(ctx)
	authorization := md.Get("authorization")
	delegations := md.Get(DelegationMetadataKey)
	if len(delegations) > 0 || delegatedAttempt(authorization) {
		if rule.delegation == nil {
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		}
		if rule.resolver.usesSVID(resolveContext) {
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		}
		return rule.delegation.admit(ctx, resolveContext, authorization, delegations)
	}
	var principal Principal
	var err error
	if rule.kind == ruleSVIDOrAccounts && rule.resolver.usesSVID(resolveContext) {
		var subject Subject
		if subject, err = rule.resolver.ResolveSubject(resolveContext); err != nil {
			return nil, err
		}
		if identity, ok := subject.(IdentityPrincipal); ok {
			return withAdmission(ctx, IdentityAdmission{Caller: identity}), nil
		}
		principal = subject.(Principal)
	} else if rule.kind == ruleAccounts || rule.kind == ruleSVIDOrAccounts {
		principal, err = rule.resolver.RequireAccount(resolveContext, rule.accounts)
	} else if rule.kind == ruleReviewedServiceAccount || rule.kind == ruleReviewedServiceAccountOrIdentity {
		var caller ReviewedCaller
		if rule.kind == ruleReviewedServiceAccount {
			caller, err = rule.resolver.ResolveReviewedCaller(resolveContext)
		} else {
			var subject ReviewedSubject
			if subject, err = rule.resolver.resolveReviewedSubject(resolveContext); err != nil {
				return nil, err
			}
			if identity, ok := subject.(IdentityPrincipal); ok {
				return withAdmission(ctx, IdentityAdmission{Caller: identity}), nil
			}
			caller = subject.(ReviewedCaller)
		}
		if err != nil {
			return nil, err
		}
		principal := caller.ServiceAccount
		admitted := withAdmission(ctx, DirectAdmission{
			Caller: principal,
			Pod:    caller.Pod,
		})
		return WithReviewedCaller(admitted, caller), nil
	} else if rule.kind == ruleAnySubject {
		var subject Subject
		if subject, err = rule.subjectResolver.ResolveSubject(resolveContext); err != nil {
			return nil, err
		}
		if identity, ok := subject.(IdentityPrincipal); ok {
			return withAdmission(ctx, IdentityAdmission{Caller: identity}), nil
		}
		principal = subject.(Principal)
	}
	if err != nil {
		return nil, err
	}
	caller := serviceAccountPrincipal(principal.Account, principal.ServiceAccountUID)
	callerContext := withAdmission(ctx, DirectAdmission{Caller: caller, Pod: &principal})
	return WithCaller(callerContext, principal), nil
}

// httpMethodName keys the one Rule AdmitRequest applies; an HTTP request has no gRPC method name.
const httpMethodName = "/http"

// AdmitRequest admits an HTTP request by this Rule exactly as the interceptors admit an RPC, from
// its Authorization header, its base64 delegation header and its verified TLS peer. The returned
// context carries the same typed Admitted result and, for a direct ServiceAccount, Caller.
func (r Rule) AdmitRequest(request *http.Request, admissionTimeout time.Duration) (context.Context, error) {
	return Methods{httpMethodName: r}.admit(requestContext(request), httpMethodName, admissionTimeout)
}

// admittedStream is a stream whose context carries the admitted principal.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }
