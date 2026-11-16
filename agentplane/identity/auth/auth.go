// Package auth authenticates workload and service SVIDs for Agentplane services.
package auth

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

// DefaultRevalidationInterval bounds how long streams wait between live credential checks.
const DefaultRevalidationInterval = 30 * time.Second

var (
	ErrInvalidBearer = errors.New("invalid workload bearer")
	ErrUnavailable   = errors.New("workload authentication unavailable")
)

var bearerCredential = regexp.MustCompile(`(?i)\ABearer +([A-Za-z0-9._~+/=-]+)\z`)

// Account names a Kubernetes ServiceAccount.
type Account struct {
	Namespace string
	Name      string
}

// UnmarshalText reads the `namespace/name` form deployment settings write an Account in.
func (a *Account) UnmarshalText(text []byte) error {
	namespace, name, found := strings.Cut(string(text), "/")
	if !found || namespace == "" || name == "" || strings.Contains(name, "/") {
		return errors.New("service account must be namespace/name")
	}
	*a = Account{Namespace: namespace, Name: name}
	return nil
}

// Principal retains an exact ServiceAccount and optional Pod binding for account attribution.
type Principal struct {
	Account           Account
	ServiceAccountUID string
	PodName           string
	PodUID            string
}

// IsPodBound reports whether the resolver proved the singular Pod binding required by
// destinations that preserve an explicit Pod-bound backend caller path.
func (p Principal) IsPodBound() bool { return p.PodName != "" && p.PodUID != "" }

// Identity is an Identity in the policy namespace, as the incarnation the caller's workload
// credential names.
type Identity struct {
	Name string
	UID  string
}

// IdentityPrincipal is a workload whose credential names an Identity, with the Pod its accepted
// incarnation runs in once its owner has named it: provenance, never the subject.
type IdentityPrincipal struct {
	Identity Identity
	Workload Workload
	PodName  string
	PodUID   string
}

// Workload is the WorkloadCredential an SVID was issued under: provenance, never the subject. A
// destination that pins state to the workload's owner (History pins a Session to its Sandbox)
// reads Owner.
type Workload struct {
	Namespace, Name string
	Owner           WorkloadOwner
}

// WorkloadOwner is a WorkloadCredential's controller: a ControllerOwner, or NoController.
type WorkloadOwner interface{ isWorkloadOwner() }

// ControllerOwner is the record's controller owner reference, such as the Sandbox whose runner the
// credential authenticates. It names an object in the record's namespace.
type ControllerOwner struct {
	APIVersion, Kind, Name, UID string
}

// NoController is a record with no controller owner, which no owner revokes: a standing
// workload, or an Identity's sandbox box, which lives until it is disposed of.
type NoController struct{}

func (ControllerOwner) isWorkloadOwner() {}
func (NoController) isWorkloadOwner()    {}

// Subject is a verified workload: a ServiceAccount Principal, or an IdentityPrincipal, which only
// a workload SVID yields.
type Subject interface{ isSubject() }

func (Principal) isSubject()         {}
func (IdentityPrincipal) isSubject() {}

// ServiceAccountPrincipal is the canonical exact ServiceAccount identity without Pod proof.
type ServiceAccountPrincipal = identitypb.ServiceAccountIdentity

func serviceAccountPrincipal(account Account, uid string) *ServiceAccountPrincipal {
	return &ServiceAccountPrincipal{
		Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name},
		Uid:     uid,
	}
}

func sameServiceAccountIdentity(left, right *ServiceAccountPrincipal) bool {
	if left == nil || right == nil {
		return left == right
	}
	leftAccount, rightAccount := left.GetAccount(), right.GetAccount()
	if leftAccount == nil || rightAccount == nil {
		return leftAccount == rightAccount && left.GetUid() == right.GetUid()
	}
	return leftAccount.GetNamespace() == rightAccount.GetNamespace() && leftAccount.GetName() == rightAccount.GetName() && left.GetUid() == right.GetUid()
}

// SubjectResolver resolves the subject admitted by an AnySubject rule, a ServiceAccount or an
// Identity. A service-owned resolver provides the verified source.
type SubjectResolver interface {
	ResolveSubject(context.Context) (Subject, error)
}

// BearerToken reads the token from exactly one RFC 6750 Bearer value. A door
// that admits more than workload tokens reads the bearer before choosing who
// verifies it.
func BearerToken(values []string) (string, error) {
	if len(values) != 1 {
		return "", ErrInvalidBearer
	}
	match := bearerCredential.FindStringSubmatch(values[0])
	if match == nil {
		return "", ErrInvalidBearer
	}
	return match[1], nil
}
