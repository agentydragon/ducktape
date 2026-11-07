package auth

import (
	"context"
	"fmt"
	"maps"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type ruleKind int

const (
	// The zero Rule refuses every caller, so a Rule left unset fails closed.
	ruleRefuse ruleKind = iota
	rulePublic
	ruleAnyAccount
	ruleAccounts
	ruleReviewedServiceAccount
)

// Rule is who may call one gRPC method.
type Rule struct {
	kind              ruleKind
	resolver          *Resolver
	principalResolver PrincipalResolver
	accounts          map[Account]struct{}
	delegation        *delegationPolicy
}

// Public admits a call without reading its bearer. Only the standard health service is public: Kubernetes
// probes carry no bearer.
func Public() Rule { return Rule{kind: rulePublic} }

// AnyAccount admits the principal its resolver proves. The handler reads it with Caller. Only a TokenReview
// Resolver also supports delegated calls.
func AnyAccount(principalResolver PrincipalResolver) Rule {
	resolver, isTokenReview := principalResolver.(*Resolver)
	if isTokenReview && resolver == nil {
		principalResolver = nil
	}
	return Rule{kind: ruleAnyAccount, resolver: resolver, principalResolver: principalResolver}
}

// Accounts admits a caller proving, by a bearer resolver reviews, one of accounts. No accounts admits nobody.
func Accounts(resolver *Resolver, accounts []Account) Rule {
	allowed := make(map[Account]struct{}, len(accounts))
	for _, account := range accounts {
		allowed[account] = struct{}{}
	}
	return Rule{kind: ruleAccounts, resolver: resolver, accounts: allowed}
}

// ReviewedServiceAccount admits any reviewed ServiceAccount in the Resolver's namespace policy.
// It stores a typed ReviewedCaller proof so account-scoped handlers can use the SA UID while
// existing Principal callers continue to require Pod-bound TokenReview claims.
func ReviewedServiceAccount(resolver *Resolver) Rule {
	return Rule{kind: ruleReviewedServiceAccount, resolver: resolver}
}

// Methods maps the full name (`/package.Service/Method`) of each method a gRPC server serves to the Rule
// admitting its callers. A method it does not list is refused before its bearer is read, so a new RPC is closed
// until its Rule is written.
type Methods map[string]Rule

// WithDelegation returns a copy of m that admits delegation only for the named full RPC methods.
// Every method must already have an authenticated direct Rule; delegations preserve that direct mode.
func (m Methods) WithDelegation(
	methodNames []string,
	delegators []Delegator,
	serviceAccounts ServiceAccountUIDReader,
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
		rule, err := rule.AllowDelegationFrom(delegators, serviceAccounts)
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

// Caller is the principal an AnyAccount or Accounts Rule admitted the call as. A call no such Rule admitted is
// refused as unauthenticated.
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
	default:
		return false
	}
}

// WithReviewedCaller stores the account proof and its separate optional Pod proof.
func WithReviewedCaller(ctx context.Context, caller ReviewedCaller) context.Context {
	return context.WithValue(ctx, reviewedCallerKey{}, caller)
}

// AdmittedReviewedCaller is available only to a method admitted by ReviewedServiceAccount.
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
	case ruleAnyAccount, ruleAccounts, ruleReviewedServiceAccount:
	default:
		return nil, errMethodNotAllowed
	}
	if rule.kind == ruleAnyAccount && rule.principalResolver == nil {
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
	if rule.kind == ruleAccounts {
		principal, err = rule.resolver.RequireAccount(resolveContext, rule.accounts)
	} else if rule.kind == ruleReviewedServiceAccount {
		var caller ReviewedCaller
		caller, err = rule.resolver.ResolveReviewedCaller(resolveContext)
		if err != nil {
			return nil, err
		}
		principal := caller.ServiceAccount
		admitted := withAdmission(ctx, DirectAdmission{
			Caller: principal,
			Pod:    caller.Pod,
		})
		return WithReviewedCaller(admitted, caller), nil
	} else {
		principal, err = rule.principalResolver.Resolve(resolveContext)
	}
	if err != nil {
		return nil, err
	}
	caller := serviceAccountPrincipal(principal.Account, principal.ServiceAccountUID)
	callerContext := withAdmission(ctx, DirectAdmission{Caller: caller, Pod: &principal})
	return WithCaller(callerContext, principal), nil
}

// admittedStream is a stream whose context carries the admitted principal.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }
