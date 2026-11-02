// Package inventory provides namespace-scoped reads of Kubernetes Sandbox resources.
package inventory

import (
	"context"
	"fmt"
	"sort"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	sandboxview "github.com/agentydragon/ducktape/agentplane/sandboxes/view"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Access reads Sandbox resources and their reported Pod incarnation in one namespace.
type Access struct {
	namespace string
	objects   client.Reader
	reader    *sandboxreader.Reader
}

func New(namespace string, objects client.Reader) (*Access, error) {
	reader, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Access{namespace: namespace, objects: objects, reader: reader}, nil
}

// ListSandboxes lists Sandbox resources and joins only the Pod UID reported as each Sandbox's
// current incarnation.
func (a *Access) ListSandboxes(ctx context.Context) ([]*sandboxespb.Sandbox, error) {
	var sandboxes sandboxapi.SandboxList
	if err := a.objects.List(ctx, &sandboxes, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list Sandboxes: %w", err)
	}
	var pods corev1.PodList
	if err := a.objects.List(ctx, &pods, client.InNamespace(a.namespace)); err != nil {
		return nil, fmt.Errorf("list Sandbox Pods: %w", err)
	}
	podsByName := make(map[string]*corev1.Pod, len(pods.Items))
	for index := range pods.Items {
		pod := &pods.Items[index]
		podsByName[pod.Name] = pod
	}
	views := make([]*sandboxespb.Sandbox, 0, len(sandboxes.Items))
	for index := range sandboxes.Items {
		resource := &sandboxes.Items[index]
		var pod *corev1.Pod
		if incarnation := resource.Status.Incarnation; incarnation != nil {
			candidate := podsByName[incarnation.Name]
			if candidate != nil && string(candidate.UID) == incarnation.UID {
				pod = candidate
			}
		}
		view, err := sandboxview.View(resource, pod)
		if err != nil {
			return nil, fmt.Errorf("view Sandbox %q: %w", resource.Name, err)
		}
		views = append(views, view)
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Name < views[j].Name })
	return views, nil
}

// Get returns one Sandbox view and its reported Pod incarnation, if the Pod still has that UID.
func (a *Access) Get(ctx context.Context, name string) (*sandboxespb.Sandbox, error) {
	resource, err := a.read(ctx, name)
	if err != nil {
		return nil, err
	}
	var pod *corev1.Pod
	if incarnation := resource.Status.Incarnation; incarnation != nil {
		candidate := &corev1.Pod{}
		err := a.objects.Get(ctx, types.NamespacedName{Namespace: a.namespace, Name: incarnation.Name}, candidate)
		if err != nil && !apierrors.IsNotFound(err) {
			return nil, fmt.Errorf("read Sandbox incarnation Pod: %w", err)
		}
		if err == nil && string(candidate.UID) == incarnation.UID {
			pod = candidate
		}
	}
	return sandboxview.View(resource, pod)
}

func (a *Access) read(ctx context.Context, name string) (*sandboxapi.Sandbox, error) {
	view, err := a.reader.Get(ctx, name)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, &SandboxNotFoundError{Name: name}
		}
		return nil, err
	}
	return view.Resource, nil
}
