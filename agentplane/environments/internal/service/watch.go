package service

import (
	"context"
	"errors"
	"log/slog"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
)

const watchWriteTimeout = 5 * time.Second

func (s *Service) WatchEnvironment(request *environmentpb.WatchEnvironmentRequest, stream grpc.ServerStreamingServer[environmentpb.WatchEnvironmentResponse]) error {
	ctx := stream.Context()
	identity, err := environmentSubject(ctx)
	if err != nil {
		return err
	}
	broad := s.broadReader(ctx, identity)
	if identity.identity == nil && !broad {
		return status.Error(codes.NotFound, "environment not found")
	}
	target := request.GetTarget()
	get := &environmentpb.GetEnvironmentRequest{Target: target}
	// Authorize the target before allocating watches or reading any collection.
	if _, _, err := s.getEnvironment(ctx, get, identity); err != nil {
		if authErr := auth.RevalidateStream(ctx); authErr != nil {
			return authErr
		}
		return err
	}
	sandboxes, err := s.watchCollection(ctx, &api.SandboxList{}, target.GetNamespace(), target.GetName())
	if err != nil {
		return err
	}
	defer sandboxes.Stop()
	var accountEvents <-chan watch.Event
	if broad {
		accounts, err := s.watchCollection(ctx, &corev1.ServiceAccountList{}, target.GetNamespace(), target.GetName())
		if err != nil {
			return err
		}
		defer accounts.Stop()
		accountEvents = accounts.ResultChan()
	}
	// A Sandbox may name a VM Pod independently of its own name. Watching this namespace-scoped
	// collection and filtering by the reported Pod name avoids missing a new incarnation between
	// opening a Pod watch and receiving the Sandbox status event that names it.
	pods, err := s.watchCollection(ctx, &corev1.PodList{}, target.GetNamespace(), "")
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
			// Identity revocation can also hide the target during this read, even
			// after the periodic credential check. Report the credential failure first.
			if authErr := auth.RevalidateStream(ctx); authErr != nil {
				return authErr
			}
			return err
		}
		if !environmentDescriptorsEqual(previous, current) {
			if err := sendEnvironmentWatch(ctx, stream.Send, &environmentpb.WatchEnvironmentResponse{Environment: current}); err != nil {
				return err
			}
			previous = current
		}
		podName = currentPodName

		// Ignore unrelated Pod events. Relevant object changes and periodic authorization
		// checks rebuild the descriptor, including its current grant-backed resume facts.
	waitForChange:
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
				break waitForChange
			case event, open = <-sandboxes.ResultChan():
			case event, open = <-accountEvents:
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

func sendEnvironmentWatch[Frame any](ctx context.Context, send func(*Frame) error, frame *Frame) error {
	if err := auth.RevalidateStream(ctx); err != nil {
		return err
	}
	err := streaming.SendWithin(ctx, watchWriteTimeout, send, frame)
	if errors.Is(err, streaming.ErrSendTimeout) {
		return status.Error(codes.DeadlineExceeded, "environment watch write deadline expired")
	}
	return err
}

func watchFailure(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	slog.ErrorContext(ctx, "Environment state watch failed", "error", err)
	return status.Error(codes.Unavailable, "environment state watch is unavailable; reconnect for a current snapshot")
}
