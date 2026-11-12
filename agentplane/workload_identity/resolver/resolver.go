// Package resolver resolves a verified workload certificate to the subject its WorkloadCredential
// names. It reads only WorkloadCredentials and the subject they name, never an owner.
package resolver

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"slices"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	// ErrRefused means the certificate names no live, current credential.
	ErrRefused = errors.New("workload credential refused")
	// ErrUnavailable means the API server could not answer; callers fail closed.
	ErrUnavailable = errors.New("workload credential lookup unavailable")
)

// Subject is who a resolved workload acts as: a ServiceAccount or an Identity, each read live with
// its UID.
type Subject interface{ subject() }

// ServiceAccount is a ServiceAccount in the record's namespace.
// CLEANUP(added 2026-11-10): remove in IDENTITY_CUTOVER (agentplane/plans/task_dag.md), with the
// WorkloadCredential's ServiceAccount subject.
type ServiceAccount struct {
	Namespace, Name string
	UID             types.UID
}

// Identity is an Identity in the policy namespace. A record naming the Identity's UID resolves only
// to that incarnation, so its credential gains nothing from a same-named successor; a record naming
// no UID (a standing record rendered from git) resolves to the live Identity of that name.
type Identity struct {
	Name string
	UID  types.UID
}

func (ServiceAccount) subject() {}
func (Identity) subject()       {}

// Peer is a resolved workload.
type Peer struct {
	Workload transport.WorkloadID
	Subject  Subject
	// Owner is the record's controller owner reference; nil for a standing workload.
	Owner *metav1.OwnerReference
	// Binding is the accepted incarnation; nil for a standing workload.
	Binding *workloadapi.Binding
	// Pod is the Pod the accepted incarnation runs in, once its owner has named it; nil for a
	// standing workload.
	Pod *workloadapi.PodRef
}

// RequirePod is step 7: a Pod-bound token the caller also offered must come from the incarnation's
// Pod. podUID is the Pod UID TokenReview reported for that token.
func (p Peer) RequirePod(podUID types.UID) error {
	if podUID == "" || p.Pod == nil || p.Pod.UID != podUID {
		return fmt.Errorf("%w: the Pod token is not bound to the credential's accepted incarnation", ErrRefused)
	}
	return nil
}

// Identities is where a credential's Identity subject lives: the policy namespace.
type Identities struct {
	PolicyNamespace string
}

// Resolver reads the WorkloadCredentials of its allowed namespaces.
type Resolver struct {
	objects    client.Reader
	namespaces []string
	identities *Identities
}

// New returns a Resolver. objects must read the API server directly: a certificate never
// preserves the authority of a deleted record, a replaced credential ID or a deleted Identity.
func New(objects client.Reader, namespaces []string, identities *Identities) (*Resolver, error) {
	if objects == nil || len(namespaces) == 0 || slices.Contains(namespaces, "") {
		return nil, errors.New("a WorkloadCredential reader and non-empty allowed namespaces are required")
	}
	if identities == nil || identities.PolicyNamespace == "" {
		return nil, errors.New("a policy namespace is required to resolve Identity subjects")
	}
	return &Resolver{
		objects: objects, namespaces: slices.Clone(namespaces),
		identities: &Identities{PolicyNamespace: identities.PolicyNamespace},
	}, nil
}

// NewKubernetes returns a Resolver over an uncached client.
func NewKubernetes(config *rest.Config, namespaces []string, identities *Identities) (*Resolver, error) {
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return nil, err
		}
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	return New(objects, namespaces, identities)
}

// Resolve takes a leaf whose chain the TLS listener verified against the workload CA, and rechecks
// it and the live record on every call. Headers, metadata and request bodies never select the
// subject.
func (r *Resolver) Resolve(ctx context.Context, leaf *x509.Certificate) (Peer, error) {
	if leaf == nil || time.Now().Before(leaf.NotBefore) || time.Now().After(leaf.NotAfter) ||
		!slices.Contains(leaf.ExtKeyUsage, x509.ExtKeyUsageClientAuth) {
		return Peer{}, fmt.Errorf("%w: not a valid client certificate", ErrRefused)
	}
	id, err := x509svid.IDFromCert(leaf)
	if err != nil {
		return Peer{}, fmt.Errorf("%w: %w", ErrRefused, err)
	}
	workload, err := transport.ParseWorkloadURI(id.String())
	if err != nil {
		return Peer{}, fmt.Errorf("%w: %w", ErrRefused, err)
	}
	if !slices.Contains(r.namespaces, workload.Namespace) {
		return Peer{}, fmt.Errorf("%w: namespace %q is not served", ErrRefused, workload.Namespace)
	}
	key := types.NamespacedName{Namespace: workload.Namespace, Name: workload.Name}
	var record workloadapi.WorkloadCredential
	if err := r.objects.Get(ctx, key, &record); apierrors.IsNotFound(err) {
		return Peer{}, fmt.Errorf("%w: no WorkloadCredential %s", ErrRefused, key)
	} else if err != nil {
		return Peer{}, fmt.Errorf("%w: read WorkloadCredential %s: %w", ErrUnavailable, key, err)
	}
	if !record.DeletionTimestamp.IsZero() {
		return Peer{}, fmt.Errorf("%w: WorkloadCredential %s is being deleted", ErrRefused, key)
	}
	if uri, _ := workload.URI(); record.Status.CredentialID != workload.CredentialID || record.Status.URI != uri {
		return Peer{}, fmt.Errorf("%w: WorkloadCredential %s no longer issues credential %s", ErrRefused, key, workload.CredentialID)
	}
	peer := Peer{Workload: workload, Owner: metav1.GetControllerOfNoCopy(&record)}
	// An owned incarnation is proven only through its accepted binding, and clearing or replacing
	// the binding revokes it before the component has rotated the ID. Only an ownerless record with
	// no binding, a standing workload, needs none.
	if peer.Owner != nil || record.Spec.Binding != nil || record.Status.AcceptedBinding != nil {
		if record.Status.AcceptedBinding == nil || record.Spec.Binding == nil || *record.Spec.Binding != *record.Status.AcceptedBinding {
			return Peer{}, fmt.Errorf("%w: WorkloadCredential %s has no accepted binding", ErrRefused, key)
		}
		binding := *record.Status.AcceptedBinding
		peer.Binding = &binding
		peer.Pod = record.Spec.Pod.DeepCopy()
	}
	subject, err := r.subject(ctx, &record)
	if err != nil {
		return Peer{}, err
	}
	peer.Subject = subject
	return peer, nil
}

// subject reads the record's subject live and checks it is still the incarnation the record names.
func (r *Resolver) subject(ctx context.Context, record *workloadapi.WorkloadCredential) (Subject, error) {
	if named := record.Spec.Subject.Identity; named != nil {
		var identity policyapi.Identity
		key := types.NamespacedName{Namespace: r.identities.PolicyNamespace, Name: named.Name}
		if err := r.objects.Get(ctx, key, &identity); apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("%w: subject Identity %s is gone", ErrRefused, key)
		} else if err != nil {
			return nil, fmt.Errorf("%w: read Identity %s: %w", ErrUnavailable, key, err)
		}
		if !identity.DeletionTimestamp.IsZero() {
			return nil, fmt.Errorf("%w: subject Identity %s is being deleted", ErrRefused, key)
		}
		// Pinning the incarnation is the requester's choice (workloadapi.IdentitySubject).
		if named.UID != "" && identity.UID != named.UID {
			return nil, fmt.Errorf("%w: subject Identity %s is not the incarnation the record names", ErrRefused, key)
		}
		return Identity{Name: identity.Name, UID: identity.UID}, nil
	}
	var account corev1.ServiceAccount
	key := types.NamespacedName{Namespace: record.Namespace, Name: record.Spec.Subject.ServiceAccount}
	if err := r.objects.Get(ctx, key, &account); apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("%w: subject ServiceAccount %s is gone", ErrRefused, key)
	} else if err != nil {
		return nil, fmt.Errorf("%w: read ServiceAccount %s: %w", ErrUnavailable, key, err)
	}
	if account.UID == "" || !account.DeletionTimestamp.IsZero() {
		return nil, fmt.Errorf("%w: subject ServiceAccount %s is being deleted", ErrRefused, key)
	}
	return ServiceAccount{Namespace: account.Namespace, Name: account.Name, UID: account.UID}, nil
}
