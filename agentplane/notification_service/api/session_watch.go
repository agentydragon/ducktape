package api

import (
	"context"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	pb "github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const sessionWatchHeartbeat = 15 * time.Second

// sessionOwner admits the configured app reader to the namespace, returned as a nil owner, and other
// callers to their own current Sandbox incarnation (currentWorkload). A missing inbox is visible only if
// the Session authority and History prove the Session belongs to that owner and incarnation; existing
// inboxes retain their established ownership even if History is down.
func (s *Notifications) sessionOwner(ctx context.Context, sessionID string) (*workload, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	if sessionID == "" || len([]rune(sessionID)) > 200 {
		return nil, invalidRequest("session_id must contain between 1 and 200 characters")
	}
	operator, err := s.operator(ctx)
	if err != nil || operator {
		return nil, err
	}
	caller, err := s.currentWorkload(ctx)
	if err != nil {
		return nil, err
	}
	found, _, err := s.store.SessionStatus(ctx, s.namespace, sessionID, &caller.owner, s.debounce, false, false)
	if err != nil {
		return nil, err
	}
	if found == nil {
		response, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: sessionID})
		if err != nil {
			return nil, store.ErrNotFound
		}
		if identity, ok := sessionidentity.Identity(response.GetSession(), sessionID, caller.sandbox); !ok || identity != caller.owner.Identity {
			return nil, store.ErrNotFound
		}

		sandbox, err := s.sandbox(ctx, caller.sandbox)
		if err != nil {
			return nil, err
		}
		if err := s.confirmSession(ctx, sandbox.Identity, sessionID); err != nil {
			return nil, err
		}
	}
	return &caller, nil
}

// inboxOwner is the store owner of a sessionOwner result: nil for the operator.
func inboxOwner(caller *workload) *store.Owner {
	if caller == nil {
		return nil
	}
	return &caller.owner
}

func (s *Notifications) GetInbox(ctx context.Context, request *pb.GetInboxRequest) (*pb.GetInboxResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	caller, err := s.sessionOwner(ctx, request.GetSessionId())
	if err != nil {
		return nil, failure(ctx, err)
	}
	current, _, err := s.store.SessionStatus(ctx, s.namespace, request.GetSessionId(), inboxOwner(caller), s.debounce, false, false)
	if err != nil {
		return nil, failure(ctx, err)
	}
	if current == nil {
		return &pb.GetInboxResponse{}, nil
	}
	view, err := sessionInboxView(*current)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return &pb.GetInboxResponse{Inbox: view}, nil
}

func sessionInboxView(inbox store.InboxStatus) (*pb.Inbox, error) {
	unannounced, err := toInt32("unannounced entries", inbox.UnannouncedCount)
	if err != nil {
		return nil, err
	}
	pending, err := toInt32("pending entries", inbox.PendingAcknowledgementCount)
	if err != nil {
		return nil, err
	}
	reason, err := sessionWaitReason(inbox.NoticeWait)
	if err != nil {
		return nil, err
	}
	view := inboxView(inbox.Inbox)
	view.LatestNotice = noticeView(inbox.Notice)
	view.Delivery = &pb.InboxDeliveryStatus{UnannouncedCount: unannounced, PendingAcknowledgementCount: pending, WaitReason: reason, NextWorkAt: timestamp(inbox.NextWorkAt)}
	if window := inbox.NoticeWait.Window; window != nil {
		view.Delivery.NoticeDueAt = timestamppb.New(window.Due)
		view.Delivery.QuietUntil = timestamppb.New(window.QuietUntil)
		view.Delivery.MaxWaitAt = timestamppb.New(window.MaxWaitAt)
	}
	return view, nil
}

func sessionWaitReason(wait store.NoticeWait) (*pb.NoticeWaitReason, error) {
	if wait.Reason == nil {
		return nil, nil
	}
	var reason pb.NoticeWaitReason
	switch *wait.Reason {
	case store.WaitRetired:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_RETIRED
	case store.WaitNoticeError:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_NOTICE_ERROR
	case store.WaitDeliveryRetry:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_DELIVERY_RETRY
	case store.WaitAwaitingConfirmation:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_AWAITING_CONFIRMATION
	case store.WaitDebouncing:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING
	case store.WaitReady:
		reason = pb.NoticeWaitReason_NOTICE_WAIT_REASON_READY
	default:
		return nil, fmt.Errorf("unknown notice wait reason %q", *wait.Reason)
	}
	return &reason, nil
}

func nextSessionChange(current *store.InboxStatus, observed time.Time) time.Time {
	if current == nil {
		return time.Time{}
	}
	var next time.Time
	consider := func(value time.Time) {
		if value.After(observed) && (next.IsZero() || value.Before(next)) {
			next = value
		}
	}
	if current.NoticeWait.Window != nil {
		consider(current.NoticeWait.Window.Due)
	}
	for _, sub := range current.Subscriptions {
		consider(sub.ExpiresAt)
		if sub.GitHub != nil {
			for _, access := range sub.GitHub.Access {
				if access.CurrentlyValid && access.ValidUntil != nil {
					consider(*access.ValidUntil)
				}
			}
		}
	}
	return next
}

// followSession subscribes before reading. A dropped listener fails the stream so a client reconnects
// and receives a fresh snapshot. A quiet stream revalidates its bearer and sends bounded heartbeats.
func followSession[T any](ctx context.Context, s *Notifications, sessionID string, caller *workload, stream grpc.ServerStreamingServer[T], read func(context.Context) (*T, time.Time, error), heartbeat *T) error {
	if s.wakeups == nil {
		return status.Error(codes.Unavailable, "notification change feed unavailable")
	}
	changes, unsubscribe, err := s.wakeups.SubscribeKey(store.InboxChannel, sessionID)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	if !s.wakeups.Connected() {
		return status.Error(codes.Unavailable, "notification change feed unavailable")
	}
	ticker := time.NewTicker(sessionWatchHeartbeat)
	defer ticker.Stop()
	needRead := true
	var due time.Time
	for {
		if !s.wakeups.Connected() {
			return status.Error(codes.Unavailable, "notification change feed unavailable")
		}
		if caller != nil {
			checkCtx, cancel := context.WithTimeout(ctx, s.timeout)
			_, err := s.sandbox(checkCtx, caller.sandbox)
			cancel()
			if err != nil {
				return failure(ctx, err)
			}
		}
		if needRead {
			readCtx, cancel := context.WithTimeout(ctx, s.timeout)
			frame, next, err := read(readCtx)
			cancel()
			if err != nil {
				return failure(ctx, err)
			}
			due = next
			needRead = false
			if frame != nil {
				if err := auth.RevalidateStream(ctx); err != nil {
					return err
				}
				if err := sendWithin(ctx, stream, s.timeout, frame); err != nil {
					return err
				}
			}
		}
		var timer *time.Timer
		var dueChan <-chan time.Time
		if !due.IsZero() {
			timer = time.NewTimer(time.Until(due))
			dueChan = timer.C
		}
		select {
		case <-ctx.Done():
			if timer != nil {
				timer.Stop()
			}
			return status.FromContextError(ctx.Err()).Err()
		case <-s.draining.Done():
			if timer != nil {
				timer.Stop()
			}
			return nil
		case <-changes:
			needRead = true
		case <-dueChan:
			needRead = true
		case <-ticker.C:
			if err := auth.RevalidateStream(ctx); err != nil {
				if timer != nil {
					timer.Stop()
				}
				return err
			}
			if err := sendWithin(ctx, stream, s.timeout, heartbeat); err != nil {
				if timer != nil {
					timer.Stop()
				}
				return err
			}
		}
		if timer != nil {
			timer.Stop()
		}
	}
}

func (s *Notifications) WatchInbox(request *pb.WatchInboxRequest, stream grpc.ServerStreamingServer[pb.WatchInboxResponse]) error {
	ctx := stream.Context()
	caller, err := s.sessionOwner(ctx, request.GetSessionId())
	if err != nil {
		return failure(ctx, err)
	}
	var previous *pb.WatchInboxResponse
	read := func(ctx context.Context) (*pb.WatchInboxResponse, time.Time, error) {
		current, observed, err := s.store.SessionStatus(ctx, s.namespace, request.GetSessionId(), inboxOwner(caller), s.debounce, false, false)
		if err != nil {
			return nil, time.Time{}, err
		}
		if current == nil {
			frame := &pb.WatchInboxResponse{State: &pb.WatchInboxResponse_NoInbox{NoInbox: &emptypb.Empty{}}}
			if proto.Equal(previous, frame) {
				return nil, time.Time{}, nil
			}
			previous = frame
			return frame, time.Time{}, nil
		}
		view, err := sessionInboxView(*current)
		if err != nil {
			return nil, time.Time{}, err
		}
		frame := &pb.WatchInboxResponse{State: &pb.WatchInboxResponse_Inbox{Inbox: view}}
		if proto.Equal(previous, frame) {
			return nil, nextSessionChange(current, observed), nil
		}
		previous = frame
		return frame, nextSessionChange(current, observed), nil
	}
	return followSession(ctx, s, request.GetSessionId(), caller, stream, read, &pb.WatchInboxResponse{State: &pb.WatchInboxResponse_Heartbeat{Heartbeat: &emptypb.Empty{}}})
}

func entrySummaryView(entry store.EntrySummary) (*pb.InboxEntrySummary, error) {
	identity, err := eventIdentityView(entry.Event)
	if err != nil {
		return nil, err
	}
	view := &pb.InboxEntrySummary{Cursor: entry.Cursor, CreatedAt: timestamppb.New(entry.CreatedAt), Event: identity, GithubConclusion: entry.GitHubConclusion}
	if entry.ActionState != nil {
		state, ok := storagecodec.ActionStateFromStored(*entry.ActionState)
		if !ok {
			return nil, fmt.Errorf("unknown Action state %q", *entry.ActionState)
		}
		view.ActionState = &state
	}
	return view, nil
}

func (s *Notifications) WatchInboxEntries(request *pb.WatchInboxEntriesRequest, stream grpc.ServerStreamingServer[pb.WatchInboxEntriesResponse]) error {
	ctx := stream.Context()
	caller, err := s.sessionOwner(ctx, request.GetSessionId())
	if err != nil {
		return failure(ctx, err)
	}
	var previous *pb.InboxEntrySummarySnapshot
	read := func(ctx context.Context) (*pb.WatchInboxEntriesResponse, time.Time, error) {
		current, observed, err := s.store.SessionStatus(ctx, s.namespace, request.GetSessionId(), inboxOwner(caller), s.debounce, true, false)
		if err != nil {
			return nil, time.Time{}, err
		}
		snapshot := &pb.InboxEntrySummarySnapshot{}
		if current != nil {
			snapshot.More = current.PendingAcknowledgementCount > int64(len(current.PendingEntries))
			for _, entry := range current.PendingEntries {
				view, err := entrySummaryView(entry)
				if err != nil {
					return nil, time.Time{}, err
				}
				snapshot.Entries = append(snapshot.Entries, view)
			}
		}
		if previous == nil {
			previous = snapshot
			return &pb.WatchInboxEntriesResponse{Frame: &pb.WatchInboxEntriesResponse_Snapshot{Snapshot: snapshot}}, nextSessionChange(current, observed), nil
		}
		delta := &pb.InboxEntrySummaryDelta{More: snapshot.More}
		moreChanged := previous.More != snapshot.More
		old := make(map[int64]*pb.InboxEntrySummary, len(previous.Entries))
		fresh := make(map[int64]*pb.InboxEntrySummary, len(snapshot.Entries))
		for _, entry := range previous.Entries {
			old[entry.Cursor] = entry
		}
		for _, entry := range snapshot.Entries {
			fresh[entry.Cursor] = entry
			if !proto.Equal(old[entry.Cursor], entry) {
				delta.Upserted = append(delta.Upserted, entry)
			}
		}
		for _, entry := range previous.Entries {
			if fresh[entry.Cursor] == nil {
				delta.RemovedCursors = append(delta.RemovedCursors, entry.Cursor)
			}
		}
		previous = snapshot
		if !moreChanged && len(delta.Upserted) == 0 && len(delta.RemovedCursors) == 0 {
			return nil, nextSessionChange(current, observed), nil
		}
		return &pb.WatchInboxEntriesResponse{Frame: &pb.WatchInboxEntriesResponse_Delta{Delta: delta}}, nextSessionChange(current, observed), nil
	}
	return followSession(ctx, s, request.GetSessionId(), caller, stream, read, &pb.WatchInboxEntriesResponse{Frame: &pb.WatchInboxEntriesResponse_Heartbeat{Heartbeat: &emptypb.Empty{}}})
}

func subscriptionInfoView(sub store.Subscription, owner bool) (*pb.Subscription, error) {
	view, err := subscriptionView(sub)
	if err != nil {
		return nil, err
	}
	if !owner {
		view.IdempotencyKey = nil
		view.Version = nil
	}
	return view, nil
}

func (s *Notifications) WatchSubscriptions(request *pb.WatchSubscriptionsRequest, stream grpc.ServerStreamingServer[pb.WatchSubscriptionsResponse]) error {
	ctx := stream.Context()
	caller, err := s.sessionOwner(ctx, request.GetSessionId())
	if err != nil {
		return failure(ctx, err)
	}
	var previous *pb.SubscriptionSnapshot
	read := func(ctx context.Context) (*pb.WatchSubscriptionsResponse, time.Time, error) {
		current, observed, err := s.store.SessionStatus(ctx, s.namespace, request.GetSessionId(), inboxOwner(caller), s.debounce, false, true)
		if err != nil {
			return nil, time.Time{}, err
		}
		snapshot := &pb.SubscriptionSnapshot{}
		if current != nil {
			for _, sub := range current.Subscriptions {
				view, err := subscriptionInfoView(sub, caller != nil)
				if err != nil {
					return nil, time.Time{}, err
				}
				snapshot.Subscriptions = append(snapshot.Subscriptions, view)
			}
		}
		if previous == nil {
			previous = snapshot
			return &pb.WatchSubscriptionsResponse{Frame: &pb.WatchSubscriptionsResponse_Snapshot{Snapshot: snapshot}}, nextSessionChange(current, observed), nil
		}
		delta := &pb.SubscriptionDelta{}
		old := make(map[string]*pb.Subscription, len(previous.Subscriptions))
		for _, sub := range previous.Subscriptions {
			old[sub.Id] = sub
		}
		for _, sub := range snapshot.Subscriptions {
			if !proto.Equal(old[sub.Id], sub) {
				delta.Upserted = append(delta.Upserted, sub)
			}
			delete(old, sub.Id)
		}
		for id := range old {
			delta.RemovedIds = append(delta.RemovedIds, id)
		}
		slices.Sort(delta.RemovedIds)
		previous = snapshot
		if len(delta.Upserted) == 0 && len(delta.RemovedIds) == 0 {
			return nil, nextSessionChange(current, observed), nil
		}
		return &pb.WatchSubscriptionsResponse{Frame: &pb.WatchSubscriptionsResponse_Delta{Delta: delta}}, nextSessionChange(current, observed), nil
	}
	return followSession(ctx, s, request.GetSessionId(), caller, stream, read, &pb.WatchSubscriptionsResponse{Frame: &pb.WatchSubscriptionsResponse_Heartbeat{Heartbeat: &emptypb.Empty{}}})
}
