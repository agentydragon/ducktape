package environments

import (
	"context"
	"errors"
	"sort"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const collectionChunk = 64

type collectionSource uint8

const (
	sandboxSource collectionSource = iota + 1
	accountSource
	podSource
)

// WatchEnvironments sends one initial snapshot followed by coalesced descriptor changes.
func (s *Service) WatchEnvironments(_ *emptypb.Empty, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentsResponse]) error {
	ctx := stream.Context()
	identity, err := environmentSubject(ctx)
	if err != nil {
		return err
	}
	if !identity.valid() {
		return status.Error(codes.Unauthenticated, "effective environment identity is incomplete")
	}
	broad := identity.serviceAccount != nil && s.broadReader(ctx, identity.serviceAccount)
	if identity.identity == nil && !broad {
		return status.Error(codes.NotFound, "environment not found")
	}
	initialSandboxes := &sandboxapi.SandboxList{}
	sandboxes, err := s.watchCollection(ctx, initialSandboxes, s.namespace, "")
	if err != nil {
		return err
	}
	defer sandboxes.Stop()
	var accountEvents <-chan watch.Event
	if broad {
		accounts, err := s.watchCollection(ctx, &corev1.ServiceAccountList{}, s.namespace, "")
		if err != nil {
			return err
		}
		defer accounts.Stop()
		accountEvents = accounts.ResultChan()
	}
	// A VM Pod may not have the Sandbox's name. Index each current incarnation by Pod name.
	pods, err := s.watchCollection(ctx, &corev1.PodList{}, s.namespace, "")
	if err != nil {
		return err
	}
	defer pods.Stop()
	incarnation := make(map[string]string)
	byPod := make(map[string]map[string]struct{})
	index := func(name, pod string) {
		if old := incarnation[name]; old != "" {
			delete(byPod[old], name)
			if len(byPod[old]) == 0 {
				delete(byPod, old)
			}
		}
		delete(incarnation, name)
		if pod != "" {
			incarnation[name] = pod
			if byPod[pod] == nil {
				byPod[pod] = make(map[string]struct{})
			}
			byPod[pod][name] = struct{}{}
		}
	}
	for i := range initialSandboxes.Items {
		item := &initialSandboxes.Items[i]
		if item.Status.Incarnation != nil {
			index(item.Name, item.Status.Incarnation.Name)
		}
	}
	current, err := s.collection(ctx, identity, broad)
	if err != nil {
		return err
	}
	previous := make(map[string]*environmentpb.EnvironmentDescriptor, len(current))
	for _, item := range current {
		previous[item.GetTarget().GetName()] = item
	}
	for start := 0; start < len(current); start += collectionChunk {
		end := min(start+collectionChunk, len(current))
		if err := sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_Snapshot{Snapshot: &environmentpb.EnvironmentChunk{Environments: current[start:end]}}}); err != nil {
			return err
		}
	}
	if err := sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}); err != nil {
		return err
	}
	// Revalidate idle streams without interrupting streams whose credentials remain valid.
	recheck := time.NewTicker(s.collectionAuthInterval)
	defer recheck.Stop()
	refresh := func(name string) error {
		item, podName, err := s.collectionItem(ctx, identity, broad, name)
		if err != nil {
			return err
		}
		index(name, podName)
		old := previous[name]
		if old != nil && (item == nil || old.GetTarget().GetUid() != item.GetTarget().GetUid()) {
			if err := sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_Removed{Removed: proto.Clone(old.GetTarget()).(*identitypb.EnvironmentTarget)}}); err != nil {
				return err
			}
			delete(previous, name)
			old = nil
		}
		if item != nil && !environmentDescriptorsEqual(old, item) {
			if err := sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_Upsert{Upsert: item}}); err != nil {
				return err
			}
			previous[name] = item
		}
		return nil
	}
	for {
		var event watch.Event
		var ok bool
		var source collectionSource
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-recheck.C:
			if err := auth.RevalidateStream(ctx); err != nil {
				return err
			}
			names := make([]string, 0, len(previous))
			for name := range previous {
				names = append(names, name)
			}
			sort.Strings(names)
			for _, name := range names {
				if err := refresh(name); err != nil {
					return err
				}
			}
			continue
		case event, ok = <-sandboxes.ResultChan():
			source = sandboxSource
		case event, ok = <-accountEvents:
			source = accountSource
		case event, ok = <-pods.ResultChan():
			source = podSource
		}
		if !ok {
			return watchFailure(ctx, errors.New("Kubernetes environment watch closed"))
		}
		if event.Type == watch.Error {
			return watchFailure(ctx, apierrors.FromObject(event.Object))
		}
		object, ok := event.Object.(metav1.Object)
		if !ok {
			return watchFailure(ctx, errors.New("Kubernetes environment watch returned a non-object"))
		}
		switch source {
		case sandboxSource, accountSource:
			if err := refresh(object.GetName()); err != nil {
				return err
			}
		case podSource:
			names := make([]string, 0, len(byPod[object.GetName()]))
			for name := range byPod[object.GetName()] {
				names = append(names, name)
			}
			sort.Strings(names)
			for _, name := range names {
				if err := refresh(name); err != nil {
					return err
				}
			}
		}
	}
}

func environmentDescriptorsEqual(left, right *environmentpb.EnvironmentDescriptor) bool {
	if left == nil || right == nil {
		return left == right
	}
	leftWithoutObservedAt := proto.Clone(left).(*environmentpb.EnvironmentDescriptor)
	rightWithoutObservedAt := proto.Clone(right).(*environmentpb.EnvironmentDescriptor)
	if state := leftWithoutObservedAt.GetObservedState(); state != nil {
		state.ObservedAt = nil
	}
	if state := rightWithoutObservedAt.GetObservedState(); state != nil {
		state.ObservedAt = nil
	}
	return proto.Equal(leftWithoutObservedAt, rightWithoutObservedAt)
}

func sendCollection(ctx context.Context, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentsResponse], frame *environmentpb.WatchEnvironmentsResponse) error {
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	err := streaming.SendWithin(ctx, watchWriteTimeout, stream.Send, frame)
	if errors.Is(err, streaming.ErrSendTimeout) {
		return status.Error(codes.DeadlineExceeded, "environment watch write deadline expired")
	}
	return err
}

func (s *Service) watchCollection(ctx context.Context, objects client.ObjectList, namespace, name string) (watch.Interface, error) {
	opts := &client.ListOptions{Namespace: namespace}
	if name != "" {
		opts.FieldSelector = fields.OneTermEqualSelector("metadata.name", name)
	}
	if err := s.objects.List(ctx, objects, opts); err != nil {
		return nil, watchFailure(ctx, err)
	}
	opts.Raw = &metav1.ListOptions{ResourceVersion: objects.GetResourceVersion()}
	result, err := s.objects.Watch(ctx, objects, opts)
	if err != nil {
		return nil, watchFailure(ctx, err)
	}
	return result, nil
}

func (s *Service) collection(ctx context.Context, caller environmentCaller, broad bool) ([]*environmentpb.EnvironmentDescriptor, error) {
	var resources []sandboxapi.Sandbox
	var list sandboxapi.SandboxList
	if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		return nil, watchFailure(ctx, err)
	}
	resources = list.Items
	sort.Slice(resources, func(i, j int) bool { return resources[i].Name < resources[j].Name })
	result := make([]*environmentpb.EnvironmentDescriptor, 0, len(resources))
	for i := range resources {
		resource := &resources[i]
		target := &sandboxreader.Identity{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)}
		var execution *sandboxreader.ServiceAccountIdentity
		if broad {
			var err error
			execution, _, err = s.sandboxes.ExecutionAccount(ctx, target)
			if err != nil {
				return nil, watchFailure(ctx, err)
			}
		} else {
			if caller.identity == nil || resource.Spec.Identity == nil || resource.Spec.Identity.Name != caller.identity.Name {
				continue
			}
			owned, err := s.ownsSandbox(ctx, caller.identity, target)
			if err != nil {
				return nil, watchFailure(ctx, err)
			}
			if !owned {
				continue
			}
		}
		value, err := s.describe(ctx, resource, execution, caller)
		if status.Code(err) == codes.NotFound {
			// The Sandbox was deleted or replaced while its eligibility was read. The
			// already-open Kubernetes watch will deliver the corresponding update.
			continue
		}
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		_, _, visible, err := s.revalidateDescriptor(ctx, value, caller, broad)
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		if !visible {
			continue
		}
		result = append(result, value)
	}
	return result, nil
}

// collectionItem rereads only the Sandbox affected by a source event. The returned Pod name is
// tracked even when the item is not currently visible, so a later incarnation event can reconcile it.
func (s *Service) collectionItem(ctx context.Context, caller environmentCaller, broad bool, name string) (*environmentpb.EnvironmentDescriptor, string, error) {
	if !broad && caller.identity == nil {
		return nil, "", nil
	}
	v, err := s.sandboxes.Get(ctx, name)
	if status.Code(err) == codes.NotFound {
		return nil, "", nil
	}
	if err != nil {
		return nil, "", watchFailure(ctx, err)
	}
	resource := v.Resource
	podName := ""
	if resource.Status.Incarnation != nil {
		podName = resource.Status.Incarnation.Name
	}
	var execution *sandboxreader.ServiceAccountIdentity
	if broad {
		execution, _, err = s.sandboxes.ExecutionAccount(ctx, v.Identity)
		if err != nil {
			return nil, "", watchFailure(ctx, err)
		}
	} else {
		if resource.Spec.Identity == nil || resource.Spec.Identity.Name != caller.identity.Name {
			return nil, podName, nil
		}
		owned, err := s.ownsSandbox(ctx, caller.identity, v.Identity)
		if err != nil {
			return nil, "", watchFailure(ctx, err)
		}
		if !owned {
			return nil, podName, nil
		}
	}
	item, err := s.describe(ctx, resource, execution, caller)
	if status.Code(err) == codes.NotFound {
		// A concurrent deletion or replacement is an inventory change, not a
		// failure of the whole collection watch. Its event will refresh this name.
		return nil, podName, nil
	}
	if err != nil {
		return nil, "", watchFailure(ctx, err)
	}
	_, _, visible, err := s.revalidateDescriptor(ctx, item, caller, broad)
	if err != nil {
		return nil, "", watchFailure(ctx, err)
	}
	if !visible {
		return nil, podName, nil
	}
	return item, podName, nil
}
