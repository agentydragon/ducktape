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
	identity, err := caller(ctx)
	if err != nil {
		return err
	}
	broad := s.broadReader(ctx, identity)
	account := serviceAccountReference(identity)
	if !broad && account.Namespace != s.namespace {
		return status.Error(codes.NotFound, "environment not found")
	}
	name := ""
	if !broad {
		name = account.Name
	}
	initialSandboxes := &sandboxapi.SandboxList{}
	sandboxes, err := s.watchCollection(ctx, initialSandboxes, s.namespace, name)
	if err != nil {
		return err
	}
	defer sandboxes.Stop()
	accounts, err := s.watchCollection(ctx, &corev1.ServiceAccountList{}, s.namespace, name)
	if err != nil {
		return err
	}
	defer accounts.Stop()
	accountEvents := accounts.ResultChan()
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

func (s *Service) collection(ctx context.Context, identity *auth.ServiceAccountPrincipal, broad bool) ([]*environmentpb.EnvironmentDescriptor, error) {
	caller := environmentCaller{serviceAccount: identity}
	var resources []sandboxapi.Sandbox
	if broad {
		var list sandboxapi.SandboxList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, watchFailure(ctx, err)
		}
		resources = list.Items
	} else {
		v, err := s.sandboxes.Get(ctx, serviceAccountReference(identity).Name)
		if status.Code(err) == codes.NotFound {
			return []*environmentpb.EnvironmentDescriptor{}, nil
		}
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		execution, found, err := s.sandboxes.ExecutionAccount(ctx, v.Identity)
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		if !found || !callerMatches(identity, execution) {
			return []*environmentpb.EnvironmentDescriptor{}, nil
		}
		resources = []sandboxapi.Sandbox{*v.Resource}
	}
	sort.Slice(resources, func(i, j int) bool { return resources[i].Name < resources[j].Name })
	result := make([]*environmentpb.EnvironmentDescriptor, 0, len(resources))
	for i := range resources {
		resource := &resources[i]
		execution, found, err := s.sandboxes.ExecutionAccount(ctx, &sandboxreader.Identity{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)})
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		if !broad && (!found || !callerMatches(identity, execution)) {
			continue
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
func (s *Service) collectionItem(ctx context.Context, identity *auth.ServiceAccountPrincipal, broad bool, name string) (*environmentpb.EnvironmentDescriptor, string, error) {
	caller := environmentCaller{serviceAccount: identity}
	if !broad && name != serviceAccountReference(identity).Name {
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
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, v.Identity)
	if err != nil {
		return nil, "", watchFailure(ctx, err)
	}
	if !broad && (!found || !callerMatches(identity, execution)) {
		return nil, podName, nil
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
