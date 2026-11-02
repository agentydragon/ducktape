package environments

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
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
	get := &environmentpb.GetEnvironmentRequest{Target: request.GetTarget()}
	// Authorize the target before allocating watches or reading either collection.
	if _, err := s.GetEnvironment(ctx, get); err != nil {
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

	var previous *environmentpb.EnvironmentDescriptor
	for {
		// Both watches precede this read, so a change concurrent with the initial snapshot
		// is either in the snapshot or wakes another read. Events are never used as authority.
		current, err := s.GetEnvironment(ctx, get)
		if err != nil {
			return err
		}
		observed := proto.Clone(current).(*environmentpb.EnvironmentDescriptor)
		observed.ObservedAt = nil
		if !proto.Equal(previous, observed) {
			if err := streaming.SendWithin(ctx, watchWriteTimeout, stream.Send, &environmentpb.WatchEnvironmentResponse{Environment: current}); err != nil {
				if errors.Is(err, streaming.ErrSendTimeout) {
					return status.Error(codes.DeadlineExceeded, "environment watch write deadline expired")
				}
				return err
			}
			previous = observed
		}

		var event watch.Event
		var open bool
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case event, open = <-sandboxes.ResultChan():
		case event, open = <-accounts.ResultChan():
		}
		if !open {
			return watchFailure(ctx, errors.New("Kubernetes environment watch closed"))
		}
		if event.Type == watch.Error {
			return watchFailure(ctx, apierrors.FromObject(event.Object))
		}
	}
}

func (s *Service) watchObjects(ctx context.Context, objects client.ObjectList, target *environmentpb.TargetHandle) (watch.Interface, error) {
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
