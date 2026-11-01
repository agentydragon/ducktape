package auth

import (
	"context"
	"errors"
	"fmt"
	"strings"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/apimachinery/pkg/util/validation"
)

const (
	// DelegationMetadataKey carries the serialized Delegation protobuf as gRPC binary metadata.
	DelegationMetadataKey = "agentplane-delegation-bin"
	delegatedScheme       = "Agentplane-Delegated "
	maxDelegationBytes    = 4096
	maxDelegatedTokenSize = 16384
	maxServiceAccountUID  = 256
)

var ErrInvalidDelegation = errors.New("invalid delegation envelope")

// ServiceAccountUIDReader returns the current UID of the named account. A missing account is not
// an infrastructure failure; it means the delegated identity cannot be admitted.
type ServiceAccountUIDReader interface {
	GetServiceAccountUID(context.Context, Account) (uid string, found bool, err error)
}

// Delegator grants one actor permission to assert callers in the listed namespaces for the method
// whose Rule it is attached to. It does not grant direct caller or privileged backend authority.
type Delegator struct {
	Account          Account
	CallerNamespaces []string
}

type delegationPolicy struct {
	resolver        *Resolver
	delegators      map[Account]map[string]struct{}
	serviceAccounts ServiceAccountUIDReader
}

// AllowDelegationFrom adds a separately authenticated delegated mode while preserving this Rule's
// direct mode. The Rule must still be placed in Methods under each full RPC method that permits it.
func (r Rule) AllowDelegationFrom(delegators []Delegator, serviceAccounts ServiceAccountUIDReader) (Rule, error) {
	if r.kind == ruleRefuse || r.kind == rulePublic || r.resolver == nil {
		return Rule{}, fmt.Errorf("delegation requires an authenticated direct rule")
	}
	if serviceAccounts == nil || len(delegators) == 0 {
		return Rule{}, fmt.Errorf("ServiceAccount reader and delegators are required")
	}
	configured := make(map[Account]map[string]struct{}, len(delegators))
	for _, delegator := range delegators {
		if !validAccount(delegator.Account) {
			return Rule{}, fmt.Errorf("delegator must be a valid ServiceAccount")
		}
		if _, ok := r.resolver.namespaces[delegator.Account.Namespace]; !ok {
			return Rule{}, fmt.Errorf("delegator namespace is outside the TokenReview resolver policy")
		}
		if _, duplicate := configured[delegator.Account]; duplicate {
			return Rule{}, fmt.Errorf("delegator ServiceAccount is repeated")
		}
		if len(delegator.CallerNamespaces) == 0 {
			return Rule{}, fmt.Errorf("delegator caller namespace scope is empty")
		}
		namespaces := make(map[string]struct{}, len(delegator.CallerNamespaces))
		for _, namespace := range delegator.CallerNamespaces {
			if len(validation.IsDNS1123Label(namespace)) != 0 {
				return Rule{}, fmt.Errorf("delegator caller namespace is invalid")
			}
			namespaces[namespace] = struct{}{}
		}
		configured[delegator.Account] = namespaces
	}
	r.delegation = &delegationPolicy{
		resolver: r.resolver, delegators: configured, serviceAccounts: serviceAccounts,
	}
	return r, nil
}

// MarshalDelegation validates and bounds a delegation envelope before serializing it for gRPC
// metadata. The same validation runs at the receiver.
func MarshalDelegation(envelope *delegationpb.Delegation) ([]byte, error) {
	if !validDelegation(envelope) {
		return nil, ErrInvalidDelegation
	}
	encoded, err := proto.Marshal(envelope)
	if err != nil || len(encoded) > maxDelegationBytes {
		return nil, ErrInvalidDelegation
	}
	return encoded, nil
}

func validDelegation(envelope *delegationpb.Delegation) bool {
	if envelope == nil || envelope.GetCaller() == nil || envelope.GetCaller().GetAccount() == nil {
		return false
	}
	caller := envelope.GetCaller()
	if !validAccount(Account{Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName()}) {
		return false
	}
	if caller.GetUid() == "" || len(caller.GetUid()) > maxServiceAccountUID {
		return false
	}
	if _, err := uuid.Parse(envelope.GetActionRequestId()); err != nil {
		return false
	}
	if _, err := uuid.Parse(envelope.GetActionExecutionId()); err != nil {
		return false
	}
	return true
}

func validAccount(account Account) bool {
	return len(validation.IsDNS1123Label(account.Namespace)) == 0 &&
		len(validation.IsDNS1123Subdomain(account.Name)) == 0
}

type admissionKey struct{}

// Admission is the typed result of a method's direct or delegated authentication mode.
// Its unexported method prevents handlers from manufacturing admitted identities.
type Admission interface{ isAdmission() }

// DirectAdmission retains the direct caller's ServiceAccount identity and optional Pod proof.
type DirectAdmission struct {
	Caller ServiceAccountPrincipal
	Pod    *Principal
}

func (DirectAdmission) isAdmission() {}

// DelegatedAdmission keeps the actor's Pod-bound proof distinct from the effective caller.
// The actor is never inserted into the legacy Caller context.
type DelegatedAdmission struct {
	Actor           Principal
	EffectiveCaller ServiceAccountPrincipal
	Provenance      *delegationpb.Delegation
}

func (DelegatedAdmission) isAdmission() {}

func withAdmission(ctx context.Context, admission Admission) context.Context {
	return context.WithValue(ctx, admissionKey{}, admission)
}

// Admitted returns the typed direct or delegated authentication result installed by Methods.
func Admitted(ctx context.Context) (Admission, error) {
	admission, ok := ctx.Value(admissionKey{}).(Admission)
	if !ok {
		return nil, errNotAdmitted
	}
	return admission, nil
}

// EffectiveCaller returns only the ServiceAccount identity used for ordinary resource checks.
// It deliberately does not return the delegated actor or Pod proof.
func EffectiveCaller(ctx context.Context) (ServiceAccountPrincipal, error) {
	admission, err := Admitted(ctx)
	if err != nil {
		return ServiceAccountPrincipal{}, err
	}
	switch call := admission.(type) {
	case DirectAdmission:
		return call.Caller, nil
	case DelegatedAdmission:
		return call.EffectiveCaller, nil
	default:
		return ServiceAccountPrincipal{}, errNotAdmitted
	}
}

func delegatedAttempt(authorization []string) bool {
	for _, value := range authorization {
		if strings.HasPrefix(value, delegatedScheme) {
			return true
		}
	}
	return false
}

func (p *delegationPolicy) admit(ctx, reviewContext context.Context, authorization, envelopes []string) (context.Context, error) {
	if len(authorization) != 1 || !strings.HasPrefix(authorization[0], delegatedScheme) || len(envelopes) != 1 {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	value := authorization[0][len(delegatedScheme):]
	if value == "" || strings.TrimSpace(value) != value || strings.ContainsAny(value, " \t\r\n") || len(value) > maxDelegatedTokenSize {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	encoded := []byte(envelopes[0])
	if len(encoded) == 0 || len(encoded) > maxDelegationBytes {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal(encoded, envelope); err != nil || !validDelegation(envelope) {
		return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
	}

	actor, err := p.resolver.ResolveToken(reviewContext, value)
	if err != nil {
		switch {
		case errors.Is(err, ErrInvalidBearer):
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		case reviewContext.Err() != nil:
			return nil, status.FromContextError(reviewContext.Err()).Err()
		case errors.Is(err, ErrUnavailable):
			return nil, status.Error(codes.Unavailable, ErrUnavailable.Error())
		default:
			return nil, status.Error(codes.Unauthenticated, ErrInvalidBearer.Error())
		}
	}
	caller := envelope.GetCaller()
	namespaces, trusted := p.delegators[actor.Account]
	if !trusted {
		return nil, status.Error(codes.PermissionDenied, "delegator is not trusted for this method")
	}
	if _, ok := namespaces[caller.GetAccount().GetNamespace()]; !ok {
		return nil, status.Error(codes.PermissionDenied, "delegated caller namespace is not allowed")
	}
	uid, found, err := p.serviceAccounts.GetServiceAccountUID(reviewContext, Account{
		Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName(),
	})
	if err != nil {
		if reviewContext.Err() != nil {
			return nil, status.FromContextError(reviewContext.Err()).Err()
		}
		return nil, status.Error(codes.Unavailable, "delegated caller identity unavailable")
	}
	if !found || uid != caller.GetUid() {
		return nil, status.Error(codes.PermissionDenied, "delegated caller identity is not current")
	}
	return withAdmission(ctx, DelegatedAdmission{
		Actor: actor,
		EffectiveCaller: ServiceAccountPrincipal{
			Account:           Account{Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName()},
			ServiceAccountUID: caller.GetUid(),
		},
		Provenance: proto.Clone(envelope).(*delegationpb.Delegation),
	}), nil
}
