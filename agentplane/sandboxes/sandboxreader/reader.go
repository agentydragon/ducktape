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
