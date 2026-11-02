package environments

import (
	"context"
	"errors"
	"log/slog"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const watchWriteTimeout = 5 * time.Second

func (s *Service) WatchEnvironment(request *environmentpb.WatchEnvironmentRequest, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentResponse]) error {
	ctx := stream.Context()
	identity, err := caller(ctx)
	if err != nil {
		return err
	}
	get := &environmentpb.GetEnvironmentRequest{Target: request.GetTarget()}
	// Authorize the target before allocating watches or reading any collection.
	if _, _, err := s.getEnvironment(ctx, get, identity); err != nil {
		return err
	}
	sandboxes, err := s.watchObjects(ctx, &api.SandboxList{}, request.GetTarget())
	if err != nil {
		return err
	}
	defer sandboxes.Stop()
	accounts, err := s.watchObjects(ctx, &corev1.ServiceAccountList{}, request.GetTarget())
	if err != nil {
		return err
	}
	defer accounts.Stop()
	// A Sandbox may name a VM Pod independently of its own name. Watching this namespace-scoped
	// collection and filtering by the reported Pod name avoids missing a new incarnation between
	// opening a Pod watch and receiving the Sandbox status event that names it.
	pods, err := s.watchCollection(ctx, &corev1.PodList{}, request.GetTarget().GetNamespace(), "")
	if err != nil {
		return err
	}
	defer pods.Stop()

	var previous *environmentpb.EnvironmentDescriptor
	podName := ""
	recheck := time.NewTicker(s.collectionAuthInterval)
	defer recheck.Stop()
	for {
		// All watches precede this read, so a change concurrent with the snapshot
		// is either in the snapshot or wakes another read. Events are never used as authority.
		current, currentPodName, err := s.getEnvironment(ctx, get, identity)
		if err != nil {
			return err
		}
		if !environmentDescriptorsEqual(previous, current) {
			if err := sendEnvironment(ctx, stream, current); err != nil {
				return err
			}
			previous = current
		}
		podName = currentPodName

		// Keep consuming unrelated Pod events and periodic authorization checks without
		// rebuilding the descriptor. Only a relevant object change needs a fresh read.
		for {
			var event watch.Event
			var open bool
			podEvent := false
			select {
			case <-ctx.Done():
				return status.FromContextError(ctx.Err()).Err()
			case <-recheck.C:
				if err := auth.RevalidateStream(ctx); err != nil {
					return err
				}
				continue
			case event, open = <-sandboxes.ResultChan():
			case event, open = <-accounts.ResultChan():
			case event, open = <-pods.ResultChan():
				podEvent = true
			}
			if !open {
				return watchFailure(ctx, errors.New("Kubernetes environment watch closed"))
			}
			if event.Type == watch.Error {
				return watchFailure(ctx, apierrors.FromObject(event.Object))
			}
			if podEvent {
				object, ok := event.Object.(metav1.Object)
				if !ok {
					return watchFailure(ctx, errors.New("Kubernetes Pod watch returned a non-object"))
				}
				if object.GetName() != podName {
					continue
				}
			}
			break
		}
	}
}

func sendEnvironment(
	ctx context.Context,
	stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentResponse],
	environment *environmentpb.EnvironmentDescriptor,
) error {
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	err := streaming.SendWithin(ctx, watchWriteTimeout, stream.Send, &environmentpb.WatchEnvironmentResponse{Environment: environment})
	if errors.Is(err, streaming.ErrSendTimeout) {
		return status.Error(codes.DeadlineExceeded, "environment watch write deadline expired")
	}
	return err
}

func (s *Service) watchObjects(ctx context.Context, objects client.ObjectList, target *identitypb.EnvironmentTarget) (watch.Interface, error) {
	options := &client.ListOptions{
		Namespace: target.GetNamespace(), FieldSelector: fields.OneTermEqualSelector("metadata.name", target.GetName()),
	}
	if err := s.objects.List(ctx, objects, options); err != nil {
		return nil, watchFailure(ctx, err)
	}
	// List's version closes the gap between reading a collection and opening its watch.
	// A compacted version ends this RPC; the client reconnects for a fresh snapshot.
	options.Raw = &metav1.ListOptions{ResourceVersion: objects.GetResourceVersion()}
	result, err := s.objects.Watch(ctx, objects, options)
	if err != nil {
		return nil, watchFailure(ctx, err)
	}
	return result, nil
}

func watchFailure(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	slog.ErrorContext(ctx, "Environment state watch failed", "error", err)
	return status.Error(codes.Unavailable, "environment state watch is unavailable; reconnect for a current snapshot")
}
