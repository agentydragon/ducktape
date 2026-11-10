package auth

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"slices"
	"strings"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// DelegationMetadataKey carries the serialized Delegation protobuf as gRPC binary metadata. An
	// HTTP request carries it as a header of the same name holding the base64 form
	// (EncodeDelegationHeader), as gRPC itself transports `-bin` metadata.
	DelegationMetadataKey = "agentplane-delegation-bin"
	// DelegatedScheme is the authorization scheme of a delegated call: the actor's own
	// destination-audience token, with the effective caller in the envelope.
	DelegatedScheme       = "Agentplane-Delegated"
	delegatedScheme       = DelegatedScheme + " "
	maxDelegationBytes    = 4096
	maxDelegatedTokenSize = 16384
	maxServiceAccountUID  = 256
	maxIdentityUID        = 256
)

var ErrInvalidDelegation = errors.New("invalid delegation envelope")

// ServiceAccountUIDReader returns the current UID of the named account. A missing account is not
// an infrastructure failure; it means the delegated identity cannot be admitted.
type ServiceAccountUIDReader interface {
	GetServiceAccountUID(context.Context, Account) (uid string, found bool, err error)
}

// ServiceAccountUIDReaderFunc adapts a Kubernetes lookup at a service's composition root.
type ServiceAccountUIDReaderFunc func(context.Context, Account) (uid string, found bool, err error)

func (read ServiceAccountUIDReaderFunc) GetServiceAccountUID(ctx context.Context, account Account) (string, bool, error) {
	return read(ctx, account)
}

// IdentityUIDReader returns the UID of the live Identity of that name in the receiver's policy
// namespace. A missing or deleting Identity is not an infrastructure failure; it means the delegated
// Identity cannot be admitted.
type IdentityUIDReader interface {
	GetIdentityUID(ctx context.Context, name string) (uid string, found bool, err error)
}

// KubernetesIdentityUIDs reads Identities in policyNamespace through objects, which must read the
// API server directly so a deleted or replaced Identity is never admitted from a cache.
func KubernetesIdentityUIDs(objects client.Reader, policyNamespace string) (IdentityUIDReader, error) {
	if objects == nil || len(validation.IsDNS1123Label(policyNamespace)) != 0 {
		return nil, errors.New("an Identity reader and a valid policy namespace are required")
	}
	return kubernetesIdentityUIDs{objects: objects, namespace: policyNamespace}, nil
}

type kubernetesIdentityUIDs struct {
	objects   client.Reader
	namespace string
}

func (r kubernetesIdentityUIDs) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	var identity policyapi.Identity
	if err := r.objects.Get(ctx, types.NamespacedName{Namespace: r.namespace, Name: name}, &identity); apierrors.IsNotFound(err) {
		return "", false, nil
	} else if err != nil {
		return "", false, err
	}
	if !identity.DeletionTimestamp.IsZero() {
		return "", false, nil
	}
	return string(identity.UID), true, nil
}

// Delegator grants one actor permission to assert callers for the method whose Rule it is attached
// to: ServiceAccounts in CallerNamespaces and, when IdentityCallers is set, Identities. It does not
// grant direct caller or privileged backend authority.
type Delegator struct {
	Account          Account
	CallerNamespaces []string
	// IdentityCallers lets the actor assert any Identity in the receiver's policy namespace. It is a
	// grant of its own: trust to assert ServiceAccounts never implies it.
	IdentityCallers bool
}

type delegatorGrant struct {
	callerNamespaces map[string]struct{}
	identityCallers  bool
}

type delegationPolicy struct {
	resolver        *Resolver
	delegators      map[Account]delegatorGrant
	serviceAccounts ServiceAccountUIDReader
	// identities is nil on a method that admits no direct Identity caller, or a receiver that
	// resolves none.
	identities IdentityUIDReader
}

// AllowDelegationFrom adds a separately authenticated delegated mode while preserving this Rule's
// direct mode. The Rule must still be placed in Methods under each full RPC method that permits it.
// serviceAccounts reads the accounts a delegator with CallerNamespaces may assert, and identities,
// nil when the receiver resolves no Identity, the Identities a delegator granted IdentityCallers may
// assert; each is required by the grants that use it. A Rule admitting no direct Identity caller
// never admits a delegated one.
func (r Rule) AllowDelegationFrom(delegators []Delegator, serviceAccounts ServiceAccountUIDReader, identities IdentityUIDReader) (Rule, error) {
	if r.kind == ruleRefuse || r.kind == rulePublic || r.resolver == nil {
		return Rule{}, fmt.Errorf("delegation requires an authenticated direct rule")
	}
	if len(delegators) == 0 {
		return Rule{}, fmt.Errorf("delegators are required")
	}
	configured := make(map[Account]delegatorGrant, len(delegators))
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
		if len(delegator.CallerNamespaces) == 0 && !delegator.IdentityCallers {
			return Rule{}, fmt.Errorf("delegator caller scope is empty")
		}
		if len(delegator.CallerNamespaces) != 0 && serviceAccounts == nil {
			return Rule{}, fmt.Errorf("delegator may assert ServiceAccounts, but the receiver resolves none")
		}
		if delegator.IdentityCallers && identities == nil {
			return Rule{}, fmt.Errorf("delegator may assert Identities, but the receiver resolves none")
		}
		namespaces := make(map[string]struct{}, len(delegator.CallerNamespaces))
		for _, namespace := range delegator.CallerNamespaces {
			if len(validation.IsDNS1123Label(namespace)) != 0 {
				return Rule{}, fmt.Errorf("delegator caller namespace is invalid")
			}
			namespaces[namespace] = struct{}{}
		}
		configured[delegator.Account] = delegatorGrant{callerNamespaces: namespaces, identityCallers: delegator.IdentityCallers}
	}
	policy := &delegationPolicy{resolver: r.resolver, delegators: configured, serviceAccounts: serviceAccounts}
	if r.admitsIdentityCallers() {
		policy.identities = identities
	}
	r.delegation = policy
	return r, nil
}

// admitsIdentityCallers is whether the Rule's direct mode admits a workload credential naming an
// Identity.
func (r Rule) admitsIdentityCallers() bool {
	switch r.kind {
	case ruleReviewedServiceAccountOrIdentity, ruleAnySubject, ruleSVIDOrAccounts:
		return true
	default:
		return false
	}
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
	if envelope == nil {
		return false
	}
	switch caller := envelope.GetEffectiveCaller().(type) {
	case *delegationpb.Delegation_Caller:
		account := caller.Caller.GetAccount()
		if account == nil || !validAccount(Account{Namespace: account.GetNamespace(), Name: account.GetName()}) ||
			caller.Caller.GetUid() == "" || len(caller.Caller.GetUid()) > maxServiceAccountUID {
			return false
		}
	case *delegationpb.Delegation_Identity:
		identity := caller.Identity
		if identity == nil || len(validation.IsDNS1123Subdomain(identity.GetName())) != 0 ||
			identity.GetUid() == "" || len(identity.GetUid()) > maxIdentityUID {
			return false
		}
	default:
		return false
	}
	switch provenance := envelope.GetProvenance().(type) {
	case *delegationpb.Delegation_Action:
		for _, id := range []string{provenance.Action.GetRequestId(), provenance.Action.GetExecutionId()} {
			if _, err := uuid.Parse(id); err != nil {
				return false
			}
		}
		return true
	case *delegationpb.Delegation_Session:
		// History and LLM ingress key the Session by its canonical spelling, so only that is valid.
		parsed, err := uuid.Parse(provenance.Session.GetSessionId())
		return err == nil && parsed.String() == provenance.Session.GetSessionId()
	default:
		return false
	}
}

// EncodeDelegationHeader is the HTTP header form of a validated envelope: standard base64, as gRPC
// transports `-bin` metadata. An HTTP receiver decodes it in its request context.
func EncodeDelegationHeader(envelope *delegationpb.Delegation) (string, error) {
	encoded, err := MarshalDelegation(envelope)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(encoded), nil
}

// decodeDelegationHeader reverses EncodeDelegationHeader, accepting padded or raw base64 as gRPC
// does. An undecodable value yields an empty envelope, which admission refuses.
func decodeDelegationHeader(value string) string {
	encoding := base64.StdEncoding
	if len(value)%4 != 0 {
		encoding = base64.RawStdEncoding
	}
	decoded, err := encoding.DecodeString(value)
	if err != nil {
		return ""
	}
	return string(decoded)
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
	Caller *ServiceAccountPrincipal
	Pod    *Principal
}

func (DirectAdmission) isAdmission() {}

// DelegatedAdmission keeps the actor's Pod-bound proof distinct from the effective caller.
// The actor is never inserted into the legacy Caller context.
type DelegatedAdmission struct {
	Actor           Principal
	EffectiveCaller *ServiceAccountPrincipal
	Provenance      *delegationpb.Delegation
}

func (DelegatedAdmission) isAdmission() {}

// DelegatedIdentityAdmission is a trusted actor's call on behalf of an Identity. The actor keeps
// its Pod-bound proof; the Identity is the exact live incarnation the actor named. Only a method
// whose direct mode admits Identity callers admits one, and EffectiveCaller refuses it, so the
// handler must read Admitted and authorize the Identity as it would a direct Identity caller.
type DelegatedIdentityAdmission struct {
	Actor           Principal
	EffectiveCaller Identity
	Provenance      *delegationpb.Delegation
}

func (DelegatedIdentityAdmission) isAdmission() {}

// IdentityAdmission is a direct caller whose workload credential names an Identity. Only an
// AnySubject method admits one, and it is never inserted into the legacy Caller context.
type IdentityAdmission struct {
	Caller IdentityPrincipal
}

func (IdentityAdmission) isAdmission() {}

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
// It deliberately does not return the delegated actor or Pod proof, and refuses an Identity caller,
// direct or delegated.
func EffectiveCaller(ctx context.Context) (*ServiceAccountPrincipal, error) {
	admission, err := Admitted(ctx)
	if err != nil {
		return nil, err
	}
	switch call := admission.(type) {
	case DirectAdmission:
		return call.Caller, nil
	case DelegatedAdmission:
		return call.EffectiveCaller, nil
	default:
		return nil, errNotAdmitted
	}
}

func delegatedAttempt(authorization []string) bool {
	return slices.ContainsFunc(authorization, func(value string) bool {
		return strings.HasPrefix(value, delegatedScheme)
	})
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
	if err := p.resolver.requireBearerSelection(reviewContext); err != nil {
		return nil, err
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
	grant, trusted := p.delegators[actor.Account]
	if !trusted {
		return nil, status.Error(codes.PermissionDenied, "delegator is not trusted for this method")
	}
	if identity := envelope.GetIdentity(); identity != nil {
		return p.admitIdentity(ctx, reviewContext, actor, grant, identity, envelope)
	}
	caller := envelope.GetCaller()
	if _, ok := grant.callerNamespaces[caller.GetAccount().GetNamespace()]; !ok {
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
		EffectiveCaller: serviceAccountPrincipal(Account{
			Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName(),
		}, caller.GetUid()),
		Provenance: proto.Clone(envelope).(*delegationpb.Delegation),
	}), nil
}

func (p *delegationPolicy) admitIdentity(
	ctx, reviewContext context.Context, actor Principal, grant delegatorGrant,
	identity *identitypb.Identity, envelope *delegationpb.Delegation,
) (context.Context, error) {
	if p.identities == nil {
		return nil, status.Error(codes.PermissionDenied, "delegated Identity callers are not admitted on this method")
	}
	if !grant.identityCallers {
		return nil, status.Error(codes.PermissionDenied, "delegator is not trusted to assert Identities")
	}
	uid, found, err := p.identities.GetIdentityUID(reviewContext, identity.GetName())
	if err != nil {
		if reviewContext.Err() != nil {
			return nil, status.FromContextError(reviewContext.Err()).Err()
		}
		return nil, status.Error(codes.Unavailable, "delegated caller identity unavailable")
	}
	if !found || uid != identity.GetUid() {
		return nil, status.Error(codes.PermissionDenied, "delegated caller identity is not current")
	}
	return withAdmission(ctx, DelegatedIdentityAdmission{
		Actor:           actor,
		EffectiveCaller: Identity{Name: identity.GetName(), UID: identity.GetUid()},
		Provenance:      proto.Clone(envelope).(*delegationpb.Delegation),
	}), nil
}
