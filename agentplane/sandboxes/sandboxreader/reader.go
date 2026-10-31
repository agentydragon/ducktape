// Package sandboxreader reads a namespace's Sandboxes by name and holds a request to the exact incarnation
// it names: a name can be reused by a later Sandbox, so the UID is what says which one was meant.
package sandboxreader

import (
	"context"
	"fmt"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
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
type Identity struct {
	Namespace string
	Name      string
	UID       string
}

// View is a Sandbox as read, with the identity that was checked complete.
type View struct {
	Identity Identity
	Resource *sandboxapi.Sandbox
}

// ServiceAccountIdentity is the controller-owned execution account for one Sandbox incarnation.
type ServiceAccountIdentity struct {
	Namespace string
	Name      string
	UID       string
}

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
		Identity: Identity{Namespace: resource.Namespace, Name: resource.Name, UID: string(resource.UID)},
		Resource: &resource,
	}, nil
}

// Incarnation reads the Sandbox a destination names, only if it is still that incarnation (`NotFound` otherwise)
// and runs as that owner: a Sandbox runs as the ServiceAccount of its own name.
func (r *Reader) Incarnation(ctx context.Context, destination *sandboxespb.SandboxDestination) (View, error) {
	if destination == nil || destination.Sandbox == "" || destination.SandboxUid == "" || destination.Owner == nil ||
		destination.Owner.Namespace == "" || destination.Owner.Name == "" {
		return View{}, status.Error(codes.InvalidArgument, "Sandbox name, UID, and owner are required")
	}
	if destination.Owner.Namespace != r.namespace || destination.Owner.Name != destination.Sandbox {
		return View{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	view, err := r.Get(ctx, destination.Sandbox)
	if err != nil {
		return View{}, err
	}
	if view.Identity.UID != destination.SandboxUid {
		return View{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	return view, nil
}

// ExecutionAccount reads the live controller-owned ServiceAccount for an exact Sandbox
// incarnation. Reader must be backed by an uncached API reader for this authorization check.
// Missing or replaced Sandboxes/accounts return found=false; API errors fail closed.
func (r *Reader) ExecutionAccount(ctx context.Context, sandbox Identity) (ServiceAccountIdentity, bool, error) {
	if sandbox.Namespace != r.namespace || sandbox.Name == "" || sandbox.UID == "" {
		return ServiceAccountIdentity{}, false, fmt.Errorf("exact Sandbox identity in namespace %q is required", r.namespace)
	}
	key := types.NamespacedName{Namespace: sandbox.Namespace, Name: sandbox.Name}
	var current sandboxapi.Sandbox
	if err := r.objects.Get(ctx, key, &current); apierrors.IsNotFound(err) {
		return ServiceAccountIdentity{}, false, nil
	} else if err != nil {
		return ServiceAccountIdentity{}, false, fmt.Errorf("read Sandbox execution owner: %w", err)
	}
	if current.Name != sandbox.Name || current.Namespace != sandbox.Namespace || string(current.UID) != sandbox.UID || current.DeletionTimestamp != nil {
		return ServiceAccountIdentity{}, false, nil
	}
	var account corev1.ServiceAccount
	if err := r.objects.Get(ctx, key, &account); apierrors.IsNotFound(err) {
		return ServiceAccountIdentity{}, false, nil
	} else if err != nil {
		return ServiceAccountIdentity{}, false, fmt.Errorf("read Sandbox execution ServiceAccount: %w", err)
	}
	controlled := false
	for _, owner := range account.OwnerReferences {
		if owner.APIVersion == sandboxapi.GroupVersion.String() && owner.Kind == "Sandbox" &&
			owner.Name == current.Name && owner.UID == current.UID && owner.Controller != nil && *owner.Controller {
			controlled = true
			break
		}
	}
	if !controlled || account.Name != sandbox.Name || account.Namespace != sandbox.Namespace || account.UID == "" || account.DeletionTimestamp != nil {
		return ServiceAccountIdentity{}, false, nil
	}
	return ServiceAccountIdentity{Namespace: account.Namespace, Name: account.Name, UID: string(account.UID)}, true, nil
}
