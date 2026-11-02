package environments

import (
	"context"
	"errors"
	"sort"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	sandboxview "github.com/agentydragon/ducktape/agentplane/sandboxes/view"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// A finite lease requires callers to reauthenticate even when inventory is idle.
const (
	collectionLease = 30 * time.Second
	collectionChunk = 64
)

// WatchEnvironments sends a complete caller-visible inventory on each change. The receiver replaces
// its view only after snapshot_complete; reconnect always starts a new snapshot.
func (s *Service) WatchEnvironments(_ *emptypb.Empty, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentsResponse]) error {
	ctx := stream.Context()
	identity, err := caller(ctx)
	if err != nil {
		return err
	}
	broad := s.broadReader(ctx, identity)
	if !broad && identity.Account.Namespace != s.namespace {
		return status.Error(codes.NotFound, "environment not found")
	}
	target := &environmentpb.TargetHandle{Namespace: s.namespace}
	if !broad {
		target.Name = identity.Account.Name
	}
	sandboxes, err := s.watchCollection(ctx, &sandboxapi.SandboxList{}, target)
	if err != nil {
		return err
	}
	defer sandboxes.Stop()
	accounts, err := s.watchCollection(ctx, &corev1.ServiceAccountList{}, target)
	if err != nil {
		return err
	}
	defer accounts.Stop()
	// VM incarnation Pod names differ from the Sandbox name; a Pod event only triggers an authorized reread.
	pods, err := s.watchCollection(ctx, &corev1.PodList{}, &environmentpb.TargetHandle{Namespace: s.namespace})
	if err != nil {
		return err
	}
	defer pods.Stop()
	expiry := time.NewTimer(collectionLease)
	defer expiry.Stop()
	var previous []*sandboxpb.Sandbox
	first := true
	for {
		current, err := s.collection(ctx, identity, broad)
		if err != nil {
			return err
		}
		same := !first && len(previous) == len(current)
		if same {
			for i := range current {
				if !proto.Equal(previous[i], current[i]) {
					same = false
					break
				}
			}
		}
		if !same {
			for start := 0; start < len(current); start += collectionChunk {
				end := min(start+collectionChunk, len(current))
				frame := &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_Snapshot{Snapshot: &environmentpb.SandboxChunk{Sandboxes: current[start:end]}}}
				if err := sendCollection(ctx, stream, frame); err != nil {
					return err
				}
			}
			if err := sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}); err != nil {
				return err
			}
			previous = current
			first = false
		}
		var event watch.Event
		var ok bool
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-expiry.C:
			return sendCollection(ctx, stream, &environmentpb.WatchEnvironmentsResponse{Frame: &environmentpb.WatchEnvironmentsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		case event, ok = <-sandboxes.ResultChan():
		case event, ok = <-accounts.ResultChan():
		case event, ok = <-pods.ResultChan():
		}
		if !ok {
			return watchFailure(ctx, errors.New("Kubernetes environment watch closed"))
		}
		if event.Type == watch.Error {
			return watchFailure(ctx, apierrors.FromObject(event.Object))
		}
	}
}

func sendCollection(ctx context.Context, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentsResponse], frame *environmentpb.WatchEnvironmentsResponse) error {
	err := streaming.SendWithin(ctx, watchWriteTimeout, stream.Send, frame)
	if errors.Is(err, streaming.ErrSendTimeout) {
		return status.Error(codes.DeadlineExceeded, "environment watch write deadline expired")
	}
	return err
}

func (s *Service) watchCollection(ctx context.Context, objects client.ObjectList, target *environmentpb.TargetHandle) (watch.Interface, error) {
	if target.Name != "" {
		return s.watchObjects(ctx, objects, target)
	}
	opts := &client.ListOptions{Namespace: target.Namespace}
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

func (s *Service) collection(ctx context.Context, identity auth.ServiceAccountPrincipal, broad bool) ([]*sandboxpb.Sandbox, error) {
	var resources []sandboxapi.Sandbox
	if broad {
		var list sandboxapi.SandboxList
		if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
			return nil, watchFailure(ctx, err)
		}
		resources = list.Items
	} else {
		v, err := s.sandboxes.Get(ctx, identity.Account.Name)
		if status.Code(err) == codes.NotFound {
			return []*sandboxpb.Sandbox{}, nil
		}
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		execution, found, err := s.sandboxes.ExecutionAccount(ctx, v.Identity)
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		if !found || !callerMatches(identity, execution) {
			return []*sandboxpb.Sandbox{}, nil
		}
		resources = []sandboxapi.Sandbox{*v.Resource}
	}
	sort.Slice(resources, func(i, j int) bool { return resources[i].Name < resources[j].Name })
	result := make([]*sandboxpb.Sandbox, 0, len(resources))
	for i := range resources {
		resource := &resources[i]
		var pod *corev1.Pod
		if resource.Status.Incarnation != nil {
			pod = &corev1.Pod{}
			err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: resource.Status.Incarnation.Name}, pod)
			if apierrors.IsNotFound(err) {
				pod = nil
			} else if err != nil {
				return nil, watchFailure(ctx, err)
			}
		}
		value, err := sandboxview.View(resource, pod)
		if err != nil {
			return nil, watchFailure(ctx, err)
		}
		result = append(result, value)
	}
	return result, nil
}
