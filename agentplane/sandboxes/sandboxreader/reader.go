// Package sandboxreader reads a namespace's Sandboxes by name and holds a request to the exact incarnation
// it names: a name can be reused by a later Sandbox, so the UID is what says which one was meant.
package sandboxreader

import (
	"context"
	"fmt"

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

// FindRunnerCredential reads the live Sandbox list and returns the exact
// incarnation that currently accepts credentialID. Deletion and stop status
// alone do not revoke an accepted credential: the controller may need the
// runner to remain authenticated while it quiesces and seals Sessions. Reader
// must use an uncached API reader so clearing or replacing the accepted
// credential, or removing the Sandbox, takes effect immediately.
func (r *Reader) FindRunnerCredential(ctx context.Context, sandboxUID, credentialID string) (View, bool, error) {
	if sandboxUID == "" || credentialID == "" {
		return View{}, false, fmt.Errorf("Sandbox UID and runner credential ID are required")
	}
	var sandboxes sandboxapi.SandboxList
	if err := r.objects.List(ctx, &sandboxes, client.InNamespace(r.namespace)); err != nil {
		return View{}, false, fmt.Errorf("list live Sandboxes: %w", err)
	}
	var found *sandboxapi.Sandbox
	for i := range sandboxes.Items {
		candidate := &sandboxes.Items[i]
		if string(candidate.UID) != sandboxUID {
			continue
		}
		if found != nil {
			return View{}, false, fmt.Errorf("multiple Sandboxes have UID %q", sandboxUID)
		}
		found = candidate
	}
	if found == nil || found.Namespace != r.namespace || found.UID == "" || found.Name == "" {
		return View{}, false, nil
	}
	credential := found.Status.RunnerCredential
	if credential == nil || credential.ID != credentialID || credential.AcceptedID != credentialID || runnerPodUID(found) == "" {
		return View{}, false, nil
	}
	return View{
		Identity: &Identity{Namespace: found.Namespace, Name: found.Name, Uid: string(found.UID)},
		Resource: found,
	}, true, nil
}

// RunnerPodUID returns the Pod bound to the Sandbox's accepted credential.
// KubeVirt credentials bind to status.incarnation (the virt-launcher Pod);
// Pod and Firecracker credentials bind to runnerCredential.podUid.
func (v View) RunnerPodUID() (string, bool) {
	_, uid, ok := v.RunnerPod()
	return uid, ok
}

// RunnerPod returns the name and UID of the Pod currently bound to the accepted
// credential. It uses the Sandbox's status incarnation for both backends;
// Pod and Firecracker credentials must agree with runnerCredential.podUid.
func (v View) RunnerPod() (name, uid string, ok bool) {
	if v.Resource == nil || v.Resource.Status.RunnerCredential == nil {
		return "", "", false
	}
	credential := v.Resource.Status.RunnerCredential
	incarnation := v.Resource.Status.Incarnation
	if incarnation == nil || incarnation.Name == "" || incarnation.UID == "" {
		return "", "", false
	}
	switch credential.Kind {
	case sandboxapi.RunnerCredentialKindPod:
		if credential.PodUID == "" || credential.PodUID != incarnation.UID {
			return "", "", false
		}
	case sandboxapi.RunnerCredentialKindVM:
		if credential.LastVMIUID == "" {
			return "", "", false
		}
	default:
		return "", "", false
	}
	return incarnation.Name, incarnation.UID, true
}

func runnerPodUID(sandbox *sandboxapi.Sandbox) string {
	view := View{Resource: sandbox}
	_, uid, ok := view.RunnerPod()
	if !ok {
		return ""
	}
	return uid
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

// RunnerCredentialAccount resolves an accepted runner credential and its owning
// ServiceAccount together. Finalization alone does not revoke the credential:
// Runtime still needs it to deliver its final History seal.
func (r *Reader) RunnerCredentialAccount(ctx context.Context, sandboxUID, credentialID string) (View, *ServiceAccountIdentity, bool, error) {
	view, found, err := r.FindRunnerCredential(ctx, sandboxUID, credentialID)
	if err != nil || !found {
		return View{}, nil, false, err
	}
	account, found, err := r.ownedExecutionAccount(ctx, view.Resource)
	return view, account, found, err
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
