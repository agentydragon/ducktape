package api

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

// NotificationsOperator is the integration app's read-only view of what a Sandbox's notifications are doing.
// Only the configured operator ServiceAccount may call it.
type NotificationsOperator struct {
	operatorpb.UnimplementedNotificationsOperatorServer
	store     *store.Store
	resolver  *auth.Resolver
	namespace string
	// callers is the operator account alone, or nobody while none is configured.
	callers  map[auth.Account]struct{}
	debounce store.Debounce
	watcher  watcher
	drain    context.CancelFunc
}

func NewNotificationsOperator(options Options) *NotificationsOperator {
	callers := map[auth.Account]struct{}{}
	if options.OperatorReader != "" {
		callers[auth.Account{Namespace: options.Namespace, Name: options.OperatorReader}] = struct{}{}
	}
	draining, drain := context.WithCancel(context.Background())
	return &NotificationsOperator{
		store: options.Store, resolver: options.Resolver, namespace: options.Namespace, callers: callers,
		debounce: options.Debounce,
		watcher: watcher{
			wakeups: options.Wakeups, timeout: options.RequestTimeout, lease: options.WatchLease, draining: draining,
		},
		drain: drain,
	}
}

// Drain ends every open watch, so a shutdown that waits for open calls is not held up until their leases.
func (o *NotificationsOperator) Drain() { o.drain() }

// admit authenticates under the request timeout; a stream carries no deadline itself.
func (o *NotificationsOperator) admit(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, o.watcher.timeout)
	defer cancel()
	if _, err := o.resolver.RequireAccount(ctx, o.callers); err != nil {
		return failure(ctx, admissionRefused{err})
	}
	return nil
}

func (o *NotificationsOperator) WatchSandboxNotifications(
	request *operatorpb.WatchSandboxNotificationsRequest,
	stream grpc.ServerStreamingServer[operatorpb.WatchSandboxNotificationsResponse],
) error {
	ctx := stream.Context()
	if err := o.admit(ctx); err != nil {
		return err
	}
	if request.GetNamespace() != o.namespace {
		return failure(ctx, store.ErrNotFound)
	}
	if request.GetName() == "" || request.GetUid() == "" {
		return failure(ctx, invalidRequest("name and uid are required"))
	}
	ref := model.DestinationRef{Namespace: request.GetNamespace(), Name: request.GetName(), UID: request.GetUid()}
	return follow(ctx, o.watcher, stream, func(ctx context.Context) (reading[operatorpb.WatchSandboxNotificationsResponse], error) {
		current, err := o.store.SandboxStatus(ctx, ref, o.debounce)
		if err != nil {
			return reading[operatorpb.WatchSandboxNotificationsResponse]{}, err
		}
		view, err := sandboxStatusView(current)
		if err != nil {
			return reading[operatorpb.WatchSandboxNotificationsResponse]{}, err
		}
		shown := proto.Clone(view).(*operatorpb.SandboxNotificationStatus)
		shown.ObservedAt = nil
		key, err := shownKey(shown, expiredSubscriptions(current))
		return reading[operatorpb.WatchSandboxNotificationsResponse]{
			frame:     &operatorpb.WatchSandboxNotificationsResponse{Frame: &operatorpb.WatchSandboxNotificationsResponse_Snapshot{Snapshot: view}},
			shown:     key,
			changesAt: sandboxChangesAt(current),
		}, err
	}, &operatorpb.WatchSandboxNotificationsResponse{
		Frame: &operatorpb.WatchSandboxNotificationsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
	})
}

func (o *NotificationsOperator) WatchSessionNotices(
	request *operatorpb.WatchSessionNoticesRequest,
	stream grpc.ServerStreamingServer[operatorpb.WatchSessionNoticesResponse],
) error {
	ctx := stream.Context()
	if err := o.admit(ctx); err != nil {
		return err
	}
	return follow(ctx, o.watcher, stream, func(ctx context.Context) (reading[operatorpb.WatchSessionNoticesResponse], error) {
		current, err := o.store.SessionNoticeStates(ctx, o.namespace, request.GetSessionIds(), o.debounce)
		if err != nil {
			return reading[operatorpb.WatchSessionNoticesResponse]{}, err
		}
		view, err := sessionNoticeStatesView(current)
		if err != nil {
			return reading[operatorpb.WatchSessionNoticesResponse]{}, err
		}
		shown := proto.Clone(view).(*operatorpb.SessionNoticeStates)
		shown.ObservedAt = nil
		key, err := shownKey(shown, nil)
		return reading[operatorpb.WatchSessionNoticesResponse]{
			frame:     &operatorpb.WatchSessionNoticesResponse{Frame: &operatorpb.WatchSessionNoticesResponse_Snapshot{Snapshot: view}},
			shown:     key,
			changesAt: noticesChangeAt(current),
		}, err
	}, &operatorpb.WatchSessionNoticesResponse{
		Frame: &operatorpb.WatchSessionNoticesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
	})
}
