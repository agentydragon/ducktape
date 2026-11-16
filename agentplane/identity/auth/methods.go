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
)

type ruleKind int

const (
	// The zero Rule refuses every caller, so a Rule left unset fails closed.
	ruleRefuse ruleKind = iota
	rulePublic
	ruleAnySubject
	ruleServices
)

// Rule is who may call one gRPC method.
type Rule struct {
	kind              ruleKind
	subjectResolver   SubjectResolver
	serviceCallers    map[string]struct{}
	serviceDelegation *serviceDelegationPolicy
}

// Public admits a call without reading its bearer. Only the standard health service is public: Kubernetes
// probes carry no bearer.
func Public() Rule { return Rule{kind: rulePublic} }

// AnySubject admits any workload this resolver authenticates, a ServiceAccount or an Identity
// subject. A ServiceAccount caller is admitted with a Principal; an Identity caller only as an
// IdentityAdmission, so the handler must read Admitted.
func AnySubject(subjectResolver SubjectResolver) Rule {
	return Rule{kind: ruleAnySubject, subjectResolver: subjectResolver}
}

// Methods maps the full name (`/package.Service/Method`) of each method a gRPC server serves to the Rule
// admitting its callers. A method it does not list is refused before its bearer is read, so a new RPC is closed
// until its Rule is written.
type Methods map[string]Rule

// WithServiceCallers adds exact direct service peers to named methods only.
func (m Methods) WithServiceCallers(methodNames []string, callers []string) (Methods, error) {
	return m.withServiceRules(methodNames, func(rule Rule) (Rule, error) { return rule.AllowServiceCallers(callers) })
}

// WithServiceDelegation adds method-scoped service actor grants independently of direct callers.
func (m Methods) WithServiceDelegation(methodNames []string, actors []ServiceDelegator, identities IdentityUIDReader) (Methods, error) {
	return m.withServiceRules(methodNames, func(rule Rule) (Rule, error) { return rule.AllowServiceDelegationFrom(actors, identities) })
}

func (m Methods) withServiceRules(methodNames []string, configure func(Rule) (Rule, error)) (Methods, error) {
	methods := make(Methods, len(m))
	maps.Copy(methods, m)
	seen := make(map[string]struct{}, len(methodNames))
	for _, method := range methodNames {
		if _, duplicate := seen[method]; duplicate {
			return nil, fmt.Errorf("service method %q is repeated", method)
		}
		seen[method] = struct{}{}
		rule, ok := methods[method]
		if !ok {
			return nil, fmt.Errorf("service method %q is not served", method)
		}
		configured, err := configure(rule)
		if err != nil {
			return nil, fmt.Errorf("service method %q: %w", method, err)
		}
		methods[method] = configured
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
	// errNotAdmitted is a handler asking for the caller of a call without a ServiceAccount admission.
	errNotAdmitted = status.Error(codes.Unauthenticated, "call not admitted")
)

type callerKey struct{}

// WithCaller is the context a call admitted as principal runs in.
func WithCaller(ctx context.Context, principal Principal) context.Context {
	return context.WithValue(ctx, callerKey{}, principal)
}

// Caller is the ServiceAccount principal retained in the context. A call without that proof is
// refused as unauthenticated; Identity callers are read with Admitted.
func Caller(ctx context.Context) (Principal, error) {
	principal, ok := ctx.Value(callerKey{}).(Principal)
	if !ok {
		return Principal{}, errNotAdmitted
	}
	return principal, nil
}

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
	case ServiceAdmission:
		second, ok := b.(ServiceAdmission)
		return ok && first == second
	case DelegatedServiceIdentityAdmission:
		second, ok := b.(DelegatedServiceIdentityAdmission)
		return ok && first.Actor == second.Actor && first.EffectiveCaller == second.EffectiveCaller && proto.Equal(first.Provenance, second.Provenance)
	case IdentityAdmission:
		second, ok := b.(IdentityAdmission)
		return ok && first == second
	default:
		return false
	}
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
		admitted, err := m.AdmitContext(stream.Context(), info.FullMethod, admissionTimeout)
		if err != nil {
			return err
		}
		return handler(server, admittedStream{ServerStream: stream, ctx: admitted})
	}
}

// AdmitContext applies one method's Rule and retains the same credential revalidation boundary as a
// server stream. A composed receiver may use it when admission is one part of a larger method gate.
func (m Methods) AdmitContext(ctx context.Context, method string, admissionTimeout time.Duration) (context.Context, error) {
	admitted, err := m.admit(ctx, method, admissionTimeout)
	if err != nil {
		return nil, err
	}
	if original, err := Admitted(admitted); err == nil {
		revalidate := func(ctx context.Context) error {
			current, err := m.admit(ctx, method, admissionTimeout)
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
	return admitted, nil
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
	case ruleAnySubject, ruleServices:
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
	if admitted, service, err := rule.admitService(ctx, resolveContext); service {
		return admitted, err
	}
	if rule.kind == ruleServices {
		return nil, status.Error(codes.Unauthenticated, "service certificate required")
	}
	md, _ := metadata.FromIncomingContext(ctx)
	authorization := md.Get("authorization")
	delegations := md.Get(DelegationMetadataKey)
	if len(delegations) > 0 || delegatedAttempt(authorization) {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	subject, err := rule.subjectResolver.ResolveSubject(resolveContext)
	if err != nil {
		return nil, err
	}
	if identity, ok := subject.(IdentityPrincipal); ok {
		return withAdmission(ctx, IdentityAdmission{Caller: identity}), nil
	}
	principal := subject.(Principal)
	caller := serviceAccountPrincipal(principal.Account, principal.ServiceAccountUID)
	callerContext := withAdmission(ctx, DirectAdmission{Caller: caller, Pod: &principal})
	return WithCaller(callerContext, principal), nil
}

// httpMethodName keys the one Rule AdmitRequest applies; an HTTP request has no gRPC method name.
const httpMethodName = "/http"

// AdmitRequest admits an HTTP request by this Rule exactly as the interceptors admit an RPC, from
// its Authorization header, its base64 delegation header and its verified TLS peer. The returned
// context carries the same typed Admitted result and, for a direct ServiceAccount, Caller.
// AdmitContext authenticates a native transport context with this rule.
func (r Rule) AdmitContext(ctx context.Context, admissionTimeout time.Duration) (context.Context, error) {
	return Methods{httpMethodName: r}.admit(ctx, httpMethodName, admissionTimeout)
}

func (r Rule) AdmitRequest(request *http.Request, admissionTimeout time.Duration) (context.Context, error) {
	return Methods{httpMethodName: r}.admit(requestContext(request), httpMethodName, admissionTimeout)
}

// admittedStream is a stream whose context carries the admitted principal.
type admittedStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s admittedStream) Context() context.Context { return s.ctx }
