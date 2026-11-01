package auth

import (
	"context"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
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
	kind       ruleKind
	resolver   *Resolver
	accounts   map[Account]struct{}
	delegation *delegationPolicy
}

// Public admits a call without reading its bearer. Only the standard health service is public: Kubernetes
// probes carry no bearer.
func Public() Rule { return Rule{kind: rulePublic} }

// AnyAccount admits a caller proving, by a bearer resolver reviews, any account resolver serves. The handler
// reads which with Caller.
func AnyAccount(resolver *Resolver) Rule { return Rule{kind: ruleAnyAccount, resolver: resolver} }

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

// ServerOptions admits each call by its method's Rule before any handler runs. admissionTimeout bounds the bearer
// review; zero leaves it bounded by the call's own deadline.
func (m Methods) ServerOptions(admissionTimeout time.Duration) []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.ChainUnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			admitted, err := m.admit(ctx, info.FullMethod, admissionTimeout)
			if err != nil {
				return nil, err
			}
			return handler(admitted, request)
		}),
		grpc.ChainStreamInterceptor(func(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			admitted, err := m.admit(stream.Context(), info.FullMethod, admissionTimeout)
			if err != nil {
				return err
			}
			return handler(server, admittedStream{ServerStream: stream, ctx: admitted})
		}),
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
	reviewContext := ctx
	if admissionTimeout > 0 {
		var cancel context.CancelFunc
		reviewContext, cancel = context.WithTimeout(ctx, admissionTimeout)
		defer cancel()
	}
	md, _ := metadata.FromIncomingContext(ctx)
	authorization := md.Get("authorization")
	delegations := md.Get(DelegationMetadataKey)
	if len(delegations) > 0 || delegatedAttempt(authorization) {
		if rule.delegation == nil {
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		}
		return rule.delegation.admit(ctx, reviewContext, authorization, delegations)
	}
	var principal Principal
	var err error
	if rule.kind == ruleAccounts {
		principal, err = rule.resolver.RequireAccount(reviewContext, rule.accounts)
	} else if rule.kind == ruleReviewedServiceAccount {
		var caller ReviewedCaller
		caller, err = rule.resolver.ResolveReviewedCaller(reviewContext)
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
		principal, err = rule.resolver.Resolve(reviewContext)
	}
	if err != nil {
		return nil, err
	}
	caller := ServiceAccountPrincipal{Account: principal.Account, ServiceAccountUID: principal.ServiceAccountUID}
	callerContext := withAdmission(ctx, DirectAdmission{Caller: caller, Pod: &principal})
	return WithCaller(callerContext, principal), nil
}

// admittedStream is a stream whose context carries the admitted principal.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }
