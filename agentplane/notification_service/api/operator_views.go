package api

import (
	"fmt"
	"time"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func waitReasonView(wait store.NoticeWait) (*operatorpb.NoticeWaitReason, error) {
	if wait.Reason == nil {
		return nil, nil
	}
	var reason operatorpb.NoticeWaitReason
	switch *wait.Reason {
	case store.WaitRetired:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_RETIRED
	case store.WaitNoticeError:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_NOTICE_ERROR
	case store.WaitDeliveryRetry:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DELIVERY_RETRY
	case store.WaitAwaitingConfirmation:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_AWAITING_CONFIRMATION
	case store.WaitDebouncing:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_DEBOUNCING
	case store.WaitReady:
		reason = operatorpb.NoticeWaitReason_NOTICE_WAIT_REASON_READY
	default:
		return nil, fmt.Errorf("notice wait reason %q is unknown", *wait.Reason)
	}
	return &reason, nil
}

func noticeProgressView(progress store.NoticeProgress) (operatorpb.NoticeProgress, error) {
	switch progress {
	case store.NoticePrepared:
		return operatorpb.NoticeProgress_NOTICE_PROGRESS_PREPARED, nil
	case store.NoticeAttempted:
		return operatorpb.NoticeProgress_NOTICE_PROGRESS_ATTEMPTED, nil
	case store.NoticeAdmitted:
		return operatorpb.NoticeProgress_NOTICE_PROGRESS_ADMITTED, nil
	case store.NoticeConfirmed:
		return operatorpb.NoticeProgress_NOTICE_PROGRESS_CONFIRMED, nil
	default:
		return 0, fmt.Errorf("notice progress %q is unknown", progress)
	}
}

func subscriptionStatusView(subscription store.SubscriptionStatus) (*operatorpb.SubscriptionStatus, error) {
	source, err := sourceView(subscription.Source)
	if err != nil {
		return nil, err
	}
	seen, err := observe(subscription.LastSuccessAt, subscription.Failure)
	if err != nil {
		return nil, err
	}
	github, err := githubStatusView(subscription.GitHub)
	if err != nil {
		return nil, err
	}
	return &operatorpb.SubscriptionStatus{
		Id: subscription.ID.String(), Source: source, Cancelled: subscription.Cancelled,
		ExpiresAt: timestamppb.New(subscription.ExpiresAt), LastSuccessAt: seen.lastSuccessAt,
		ErrorKind: seen.errorKind, Error: seen.errorMessage, ErrorSince: seen.errorSince, ErrorObservedAt: seen.errorObserved,
		NextSourceCheckAt: timestamp(subscription.NextSourceCheckAt), Github: github,
	}, nil
}

func inboxStatusView(status store.InboxStatus) (*operatorpb.InboxStatus, error) {
	unannounced, err := toInt32("unannounced entries", status.UnannouncedCount)
	if err != nil {
		return nil, err
	}
	pending, err := toInt32("pending entries", status.PendingAcknowledgementCount)
	if err != nil {
		return nil, err
	}
	reason, err := waitReasonView(status.NoticeWait)
	if err != nil {
		return nil, err
	}
	view := &operatorpb.InboxStatus{
		Inbox: inboxView(status.Inbox), Notice: noticeView(status.Notice),
		UnannouncedCount: unannounced, PendingAcknowledgementCount: pending,
		PendingEntriesMore: status.PendingAcknowledgementCount > int64(len(status.PendingEntries)),
		NoticeWaitReason:   reason, NextWorkAt: timestamp(status.NextWorkAt),
	}
	if window := status.NoticeWait.Window; window != nil {
		view.NoticeDueAt = timestamppb.New(window.Due)
		view.QuietUntil = timestamppb.New(window.QuietUntil)
		view.MaxWaitAt = timestamppb.New(window.MaxWaitAt)
	}
	for _, subscription := range status.Subscriptions {
		subscriptionView, err := subscriptionStatusView(subscription)
		if err != nil {
			return nil, err
		}
		view.Subscriptions = append(view.Subscriptions, subscriptionView)
	}
	for _, entry := range status.PendingEntries {
		view.PendingEntries = append(view.PendingEntries, &operatorpb.InboxEntrySummary{
			Cursor: entry.Cursor, CreatedAt: timestamppb.New(entry.CreatedAt), Provider: string(entry.Provider), Summary: entry.Summary,
		})
	}
	return view, nil
}

func sandboxStatusView(status store.SandboxNotificationStatus) (*operatorpb.SandboxNotificationStatus, error) {
	view := &operatorpb.SandboxNotificationStatus{ObservedAt: timestamppb.New(status.ObservedAt)}
	for _, inbox := range status.Inboxes {
		inboxView, err := inboxStatusView(inbox)
		if err != nil {
			return nil, err
		}
		view.Inboxes = append(view.Inboxes, inboxView)
	}
	return view, nil
}

func inboxNoticeStateView(state store.InboxNoticeState) (*operatorpb.InboxNoticeState, error) {
	queued, err := toInt32("queued entries", state.Queued)
	if err != nil {
		return nil, err
	}
	unacknowledged, err := toInt32("unacknowledged entries", state.Unacknowledged)
	if err != nil {
		return nil, err
	}
	reason, err := waitReasonView(state.NoticeWait)
	if err != nil {
		return nil, err
	}
	view := &operatorpb.InboxNoticeState{
		InboxId: state.InboxID.String(), SessionId: state.SessionID, Queued: queued, Unacknowledged: unacknowledged,
		NoticeWaitReason: reason, Error: state.Error,
	}
	if state.Notice != nil {
		progress, err := noticeProgressView(*state.Notice)
		if err != nil {
			return nil, err
		}
		view.Notice = &progress
	}
	if window := state.NoticeWait.Window; window != nil {
		view.EligibleAt = timestamppb.New(window.Due)
	}
	return view, nil
}

func sessionNoticeStatesView(states store.SessionNoticeStates) (*operatorpb.SessionNoticeStates, error) {
	view := &operatorpb.SessionNoticeStates{ObservedAt: timestamppb.New(states.ObservedAt)}
	for _, inbox := range states.Inboxes {
		inboxView, err := inboxNoticeStateView(inbox)
		if err != nil {
			return nil, err
		}
		view.Inboxes = append(view.Inboxes, inboxView)
	}
	return view, nil
}

// sandboxChangesAt is the next time after the read at which the status shown changes without a commit: when an
// inbox's notice falls due, and when a live subscription expires. The zero time is none.
func sandboxChangesAt(status store.SandboxNotificationStatus) time.Time {
	var next time.Time
	consider := func(deadline time.Time) {
		if deadline.After(status.ObservedAt) && (next.IsZero() || deadline.Before(next)) {
			next = deadline
		}
	}
	for _, inbox := range status.Inboxes {
		if window := inbox.NoticeWait.Window; window != nil {
			consider(window.Due)
		}
		for _, subscription := range inbox.Subscriptions {
			if !subscription.Cancelled {
				consider(subscription.ExpiresAt)
			}
		}
	}
	return next
}

// expiredSubscriptions says which subscriptions have expired at the read, in the order the status lists them: an
// expiry changes what is shown though no row changed.
func expiredSubscriptions(status store.SandboxNotificationStatus) []bool {
	var expired []bool
	for _, inbox := range status.Inboxes {
		for _, subscription := range inbox.Subscriptions {
			expired = append(expired, !subscription.ExpiresAt.After(status.ObservedAt))
		}
	}
	return expired
}

// noticesChangeAt is the next time after the read at which a debounced notice of one of the inboxes falls due.
func noticesChangeAt(states store.SessionNoticeStates) time.Time {
	var next time.Time
	for _, inbox := range states.Inboxes {
		if window := inbox.NoticeWait.Window; window != nil && window.Due.After(states.ObservedAt) && (next.IsZero() || window.Due.Before(next)) {
			next = window.Due
		}
	}
	return next
}
