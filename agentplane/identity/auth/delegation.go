package auth

import (
	"context"
	"encoding/base64"
	"errors"
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
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// DelegationMetadataKey carries the serialized Delegation protobuf as gRPC binary metadata. An
	// HTTP request carries it as a header of the same name holding the base64 form
	// (EncodeDelegationHeader), as gRPC itself transports `-bin` metadata.
	DelegationMetadataKey = "agentplane-delegation-bin"
	// DelegatedScheme is the authorization scheme of a delegated call: the actor's own
	// destination-audience token, with the effective caller in the envelope.
	DelegatedScheme    = "Agentplane-Delegated"
	delegatedScheme    = DelegatedScheme + " "
	maxDelegationBytes = 4096
	maxIdentityUID     = 256
)

var ErrInvalidDelegation = errors.New("invalid delegation envelope")

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

// NewKubernetesIdentityUIDs creates an uncached reader of policyNamespace's Identities for a service's
// delegation boundary.
func NewKubernetesIdentityUIDs(config *rest.Config, policyNamespace string) (IdentityUIDReader, error) {
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		return nil, err
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	return KubernetesIdentityUIDs(objects, policyNamespace)
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

// admitsIdentityCallers is whether the Rule's direct mode admits a workload credential naming an
// Identity.
func (r Rule) admitsIdentityCallers() bool {
	switch r.kind {
	case ruleAnySubject:
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
	case *delegationpb.Delegation_Connection:
		if provenance.Connection == nil {
			return false
		}
		// Connection identifiers are canonical UUIDs. They identify durable OAuth authority for
		// audit attribution, but the receiver still relies on its method-scoped actor grant and the
		// edge's live grant revalidation; neither identifier is authority on its own.
		for _, id := range []string{provenance.Connection.GetConnectionId(), provenance.Connection.GetGrantId()} {
			parsed, err := uuid.Parse(id)
			if err != nil || parsed.String() != id {
				return false
			}
		}
		return true
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
	default:
		return nil, errNotAdmitted
	}
}

func delegatedAttempt(authorization []string) bool {
	return slices.ContainsFunc(authorization, func(value string) bool {
		return strings.HasPrefix(value, delegatedScheme)
	})
}

func resolveDelegatedIdentity(ctx context.Context, identities IdentityUIDReader, identity *identitypb.Identity) (Identity, error) {
	if identities == nil {
		return Identity{}, status.Error(codes.PermissionDenied, "delegated Identity callers are not admitted on this method")
	}
	uid, found, err := identities.GetIdentityUID(ctx, identity.GetName())
	if err != nil {
		if ctx.Err() != nil {
			return Identity{}, status.FromContextError(ctx.Err()).Err()
		}
		return Identity{}, status.Error(codes.Unavailable, "delegated caller identity unavailable")
	}
	if !found || uid != identity.GetUid() {
		return Identity{}, status.Error(codes.PermissionDenied, "delegated caller identity is not current")
	}
	return Identity{Name: identity.GetName(), UID: identity.GetUid()}, nil
}
