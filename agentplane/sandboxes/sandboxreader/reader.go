// Package sandboxreader reads a namespace's Sandboxes by name and holds a request to the exact incarnation
// it names: a name can be reused by a later Sandbox, so the UID is what says which one was meant.
package sandboxreader

import (
	"context"
	"fmt"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Identity is the exact Sandbox incarnation named by an authenticated runner or proxy
// request. Namespace/name alone are not sufficient because Kubernetes permits name reuse.
type Identity = identitypb.EnvironmentTarget

// View is a Sandbox as read, with the identity that was checked complete.
type View struct {
	Identity *Identity
	Resource *sandboxapi.Sandbox
}

// ServiceAccountIdentity is the controller-owned execution account for one Sandbox incarnation.
type ServiceAccountIdentity = identitypb.ServiceAccountIdentity

// Reader reads the Sandboxes of one namespace.
type Reader struct {
	namespace string
	objects   client.Reader
}

func New(namespace string, objects client.Reader) (*Reader, error) {
	if namespace == "" || objects == nil {
		return nil, fmt.Errorf("Sandbox namespace and Kubernetes client are required")
	}
	return &Reader{namespace: namespace, objects: objects}, nil
}

// Get reads the named Sandbox: `NotFound` when there is none, `Unavailable` when what is stored has no usable identity.
func (r *Reader) Get(ctx context.Context, name string) (View, error) {
	var resource sandboxapi.Sandbox
	err := r.objects.Get(ctx, types.NamespacedName{Namespace: r.namespace, Name: name}, &resource)
	if apierrors.IsNotFound(err) {
		return View{}, status.Error(codes.NotFound, "sandbox not found")
	}
	if err != nil {
		return View{}, fmt.Errorf("read Sandbox: %w", err)
	}
	if resource.UID == "" || resource.Name == "" || resource.Namespace != r.namespace {
		return View{}, status.Error(codes.Unavailable, "Sandbox identity is incomplete")
	}
	return View{
		Identity: &Identity{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)},
		Resource: &resource,
	}, nil
}

// Incarnation reads the Sandbox a destination names, only if it is still that incarnation (`NotFound` otherwise)
// and runs as that owner: a Sandbox runs as the ServiceAccount of its own name.
func (r *Reader) Incarnation(ctx context.Context, destination *identitypb.SandboxDestination) (View, error) {
	if destination == nil || destination.Target == nil || destination.Target.Namespace == "" || destination.Target.Name == "" || destination.Target.Uid == "" || destination.Owner == nil ||
		destination.Owner.Namespace == "" || destination.Owner.Name == "" {
		return View{}, status.Error(codes.InvalidArgument, "Sandbox name, UID, and owner are required")
	}
	if destination.Target.Namespace != destination.Owner.Namespace || destination.Owner.Namespace != r.namespace || destination.Owner.Name != destination.Target.Name {
		return View{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	view, err := r.Get(ctx, destination.Target.Name)
	if err != nil {
		return View{}, err
	}
	if view.Identity.Uid != destination.Target.Uid {
		return View{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	return view, nil
}

// ExecutionAccount reads the live controller-owned ServiceAccount for an exact Sandbox
// incarnation. Reader must be backed by an uncached API reader for this authorization check.
// Missing or replaced Sandboxes/accounts return found=false; API errors fail closed.
func (r *Reader) ExecutionAccount(ctx context.Context, sandbox *Identity) (*ServiceAccountIdentity, bool, error) {
	if sandbox == nil {
		return nil, false, fmt.Errorf("exact Sandbox identity in namespace %q is required", r.namespace)
	}
	if sandbox.Namespace != r.namespace || sandbox.Name == "" || sandbox.Uid == "" {
		return nil, false, fmt.Errorf("exact Sandbox identity in namespace %q is required", r.namespace)
	}
	key := types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name}
	var current sandboxapi.Sandbox
	if err := r.objects.Get(ctx, key, &current); apierrors.IsNotFound(err) {
		return nil, false, nil
	} else if err != nil {
		return nil, false, fmt.Errorf("read Sandbox execution owner: %w", err)
	}
	if current.Name != sandbox.Name || current.Namespace != sandbox.Namespace || string(current.UID) != sandbox.Uid || current.DeletionTimestamp != nil {
		return nil, false, nil
	}
	return r.ownedExecutionAccount(ctx, &current)
}

// ExecutionIdentity reads the Identity an exact live Sandbox incarnation names in `spec.identity`,
// with the UID of the Identity's current incarnation. It returns nil and found=true when the Sandbox
// names none; found=false when the Sandbox incarnation is gone or deleting or the Identity it names
// is absent or deleting. Like ExecutionAccount, Reader must read the API server directly.
func (r *Reader) ExecutionIdentity(ctx context.Context, sandbox *Identity) (*identitypb.Identity, bool, error) {
	if sandbox == nil || sandbox.Namespace != r.namespace || sandbox.Name == "" || sandbox.Uid == "" {
		return nil, false, fmt.Errorf("exact Sandbox identity in namespace %q is required", r.namespace)
	}
	var current sandboxapi.Sandbox
	if err := r.objects.Get(ctx, types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name}, &current); apierrors.IsNotFound(err) {
		return nil, false, nil
	} else if err != nil {
		return nil, false, fmt.Errorf("read Sandbox execution identity: %w", err)
	}
	if string(current.UID) != sandbox.Uid || current.DeletionTimestamp != nil {
		return nil, false, nil
	}
	if current.Spec.Identity == nil {
		return nil, true, nil
	}
	// spec.identity names an Identity in the Sandbox's namespace, while SVID verifiers read Identities
	// in the policy namespace. Assumption: the two are the same namespace, as staging renders them
	// (cluster/cdk8s/agentplane: both are the environment namespace); name and UID still match exactly.
	var named policyapi.Identity
	if err := r.objects.Get(ctx, types.NamespacedName{Namespace: current.Namespace, Name: current.Spec.Identity.Name}, &named); apierrors.IsNotFound(err) {
		return nil, false, nil
	} else if err != nil {
		return nil, false, fmt.Errorf("read Sandbox Identity: %w", err)
	}
	if named.DeletionTimestamp != nil {
		return nil, false, nil
	}
	if named.UID == "" {
		return nil, false, fmt.Errorf("Identity %q has no UID", named.Name)
	}
	return &identitypb.Identity{Name: named.Name, Uid: string(named.UID)}, true, nil
}

func (r *Reader) ownedExecutionAccount(ctx context.Context, current *sandboxapi.Sandbox) (*ServiceAccountIdentity, bool, error) {
	var account corev1.ServiceAccount
	if err := r.objects.Get(ctx, client.ObjectKeyFromObject(current), &account); apierrors.IsNotFound(err) {
		return nil, false, nil
	} else if err != nil {
		return nil, false, fmt.Errorf("read Sandbox execution ServiceAccount: %w", err)
	}
	controlled := false
	for _, owner := range account.OwnerReferences {
		if owner.APIVersion == sandboxapi.GroupVersion.String() && owner.Kind == "Sandbox" &&
			owner.Name == current.Name && owner.UID == current.UID && owner.Controller != nil && *owner.Controller {
			controlled = true
			break
		}
	}
	if !controlled || account.Name != current.Name || account.Namespace != current.Namespace || account.UID == "" || account.DeletionTimestamp != nil {
		return nil, false, nil
	}
	return &ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: string(account.UID)}, true, nil
}
