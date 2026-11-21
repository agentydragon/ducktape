package inbox

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// maxPendingEntrySummaries bounds the retained entries returned by an inbox watch.
const maxPendingEntrySummaries = 100

// Debounce is when entries are announced to the agent: a quiet window after the newest entry no notice covers,
// but never more than MaxWait after the oldest.
type Debounce struct {
	Quiet, MaxWait time.Duration
}

// Window is when the entries of EntryTimes become due for a notice, and the deadlines that set it.
type Window struct {
	QuietUntil, MaxWaitAt time.Time
	// Due is the earlier of the two deadlines.
	Due time.Time
}

// Window is when entries committed at `times` become due for a notice.
func (d Debounce) Window(times EntryTimes) Window {
	window := Window{QuietUntil: times.Last.Add(d.Quiet), MaxWaitAt: times.First.Add(d.MaxWait)}
	window.Due = window.QuietUntil
	if window.MaxWaitAt.Before(window.Due) {
		window.Due = window.MaxWaitAt
	}
	return window
}

// NoticeWaitReason is why an inbox's next notice waits.
type NoticeWaitReason string

const (
	WaitRetired              NoticeWaitReason = "retired"
	WaitNoticeError          NoticeWaitReason = "notice_error"
	WaitDeliveryRetry        NoticeWaitReason = "delivery_retry"
	WaitAwaitingConfirmation NoticeWaitReason = "awaiting_confirmation"
	WaitDebouncing           NoticeWaitReason = "debouncing"
	WaitReady                NoticeWaitReason = "ready"
)

// NoticeWait is why an inbox's next notice waits.
type NoticeWait struct {
	// Reason is nil when no entry awaits a notice and no notice or delivery problem holds the inbox.
	Reason *NoticeWaitReason
	// Window is set only while Reason is WaitDebouncing: an estimate, since a new entry extends the quiet window.
	Window *Window
}

// noticeWait judges an inbox at `observed`. pending bounds the entries no notice covers.
func noticeWait(inbox Inbox, notice *Notice, pending *EntryTimes, observed time.Time, debounce Debounce) NoticeWait {
	var window *Window
	if pending != nil {
		computed := debounce.Window(*pending)
		window = &computed
	}
	var reason NoticeWaitReason
	switch {
	case inbox.Retired:
		reason = WaitRetired
	case notice != nil && notice.Error != nil:
		reason = WaitNoticeError
	case inbox.DeliveryError != nil:
		reason = WaitDeliveryRetry
	case notice != nil && !notice.Confirmed:
		reason = WaitAwaitingConfirmation
	case window != nil && window.Due.After(observed):
		reason = WaitDebouncing
	case window != nil:
		reason = WaitReady
	default:
		return NoticeWait{}
	}
	if reason != WaitDebouncing {
		window = nil
	}
	return NoticeWait{Reason: &reason, Window: window}
}

// entriesBeyond is the commit times and the number of the inbox's entries with a cursor after `after`, with nil
// times when there are none.
func entriesBeyond(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, after int64) (*EntryTimes, int64, error) {
	var first, last sql.NullTime
	var count int64
	if err := tx.QueryRowContext(ctx,
		`SELECT min(created_at), max(created_at), count(*) FROM entry WHERE inbox_id = $1 AND cursor > $2`,
		inboxID, after).Scan(&first, &last, &count); err != nil || count == 0 {
		return nil, 0, err
	}
	return &EntryTimes{First: first.Time.UTC(), Last: last.Time.UTC()}, count, nil
}

// Status needs only Action and mergeability lifecycle fields. Message summaries use the authenticated sender in the
// identity, so the content projection leaves message_body and from_session NULL and the adapter bypasses payload decoding.
const entryContentSummaryReadColumns = `action_state, action_at,
	action_actor_kind, action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject, action_actor_identity_name, action_actor_identity_uid,
	mergeability_repository, mergeability_base_ref, mergeability_head_sha, mergeability_base_sha,
	mergeability_previous, mergeability_current, mergeability_observed_at, mergeability_observed_at_submicro_nanos,
	merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha, NULL::text, NULL::text`

// InboxStatus is one Session's inbox, with what holds its next notice.
type InboxStatus struct {
	Inbox         Inbox
	Notice        *Notice
	Subscriptions []Subscription
	// UnannouncedCount is the entries no notice covers yet, and PendingAcknowledgementCount those the agent has
	// not acknowledged, covered or not.
	UnannouncedCount            int64
	PendingAcknowledgementCount int64
	// PendingEntries are the oldest entries awaiting acknowledgement, at most 100; more are pending when
	// PendingAcknowledgementCount is greater than their number.
	PendingEntries []sourceadapter.EntrySummary
	NoticeWait     NoticeWait
	// NextWorkAt is when a worker next claims the inbox, nil for a retired inbox.
	NextWorkAt *time.Time
}

// SessionStatus reads the one inbox for a Session. The service database scopes the operator; a workload
// is additionally scoped by its exact execution Identity. Missing and unauthorized inboxes both return nil.
func (s *Store) SessionStatus(ctx context.Context, sessionID string, owner *Owner, debounce Debounce, entries, subscriptions bool) (*InboxStatus, time.Time, error) {
	observed := now()
	var result *InboxStatus
	err := s.transaction(ctx, func(tx transaction.Tx) error {
		query := `SELECT ` + inboxColumns + `, next_attempt FROM inbox WHERE session_id = $1`
		args := []any{sessionID}
		if owner != nil {
			predicate, ownerArgs := ownerPredicate(*owner, "", 2)
			query += ` AND ` + predicate
			args = append(args, ownerArgs...)
		}
		var next sql.NullTime
		inbox, err := scanInbox(tx.QueryRowContext(ctx, query, args...), &next)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		var nextWork *time.Time
		if next.Valid {
			nextWork = utc(next.Time)
		}
		value, err := readInboxStatus(ctx, tx, inbox, nextWork, observed, debounce, entries, subscriptions)
		if err != nil {
			return err
		}
		result = &value
		return nil
	})
	return result, observed, err
}

func readInboxStatus(ctx context.Context, tx transaction.Tx, inbox Inbox, nextWork *time.Time, observed time.Time, debounce Debounce, entries, subscriptions bool) (InboxStatus, error) {
	result := InboxStatus{Inbox: inbox, NextWorkAt: nextWork}
	var err error
	if subscriptions {
		if result.Subscriptions, err = subscriptionStatuses(ctx, tx, inbox.ID, observed); err != nil {
			return InboxStatus{}, err
		}
	}
	if result.Notice, err = latestNotice(ctx, tx, inbox.ID); err != nil {
		return InboxStatus{}, err
	}
	unannounced, count, err := entriesBeyond(ctx, tx, inbox.ID, max(inbox.Covered, inbox.Acknowledged, inbox.ExpiredThrough))
	if err != nil {
		return InboxStatus{}, err
	}
	result.UnannouncedCount = count
	result.NoticeWait = noticeWait(inbox, result.Notice, unannounced, observed, debounce)
	awaiting := max(inbox.Acknowledged, inbox.ExpiredThrough)
	if _, result.PendingAcknowledgementCount, err = entriesBeyond(ctx, tx, inbox.ID, awaiting); err != nil {
		return InboxStatus{}, err
	}
	if entries {
		if result.PendingEntries, err = pendingEntrySummaries(ctx, tx, inbox.ID, awaiting); err != nil {
			return InboxStatus{}, err
		}
	}
	return result, nil
}

func subscriptionStatuses(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, observed time.Time) ([]Subscription, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT `+subscriptionColumns+` FROM subscription s WHERE s.inbox_id = $1 ORDER BY s.id`, inboxID)
	if err != nil {
		return nil, err
	}
	return readSubscriptions(ctx, tx, rows, observed)
}

func pendingEntrySummaries(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, after int64) ([]sourceadapter.EntrySummary, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, `+storagecodec.EntryIdentityReadColumns+`,
			payload, `+entryContentSummaryReadColumns+`, created_at FROM entry
			WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inboxID, after, maxPendingEntrySummaries)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var summaries []sourceadapter.EntrySummary
	for rows.Next() {
		var cursor int64
		var identityRead storagecodec.EntryIdentityStorage
		var payload []byte
		var content model.EntryContentStorage
		var createdAt time.Time
		destinations := append([]any{&cursor}, storagecodec.EntryIdentityDestinations(&identityRead)...)
		destinations = append(destinations, &payload)
		destinations = append(destinations, storagecodec.EntryContentDestinations(&content)...)
		destinations = append(destinations, &createdAt)
		if err := rows.Scan(destinations...); err != nil {
			return nil, err
		}
		identity, err := sourceadapter.EventIdentityFromStorage(identityRead)
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inboxID, cursor, err)
		}
		summary, err := sourceadapter.SummarizeStoredEntry(cursor, identity, payload, content, createdAt)
		if err != nil {
			return nil, fmt.Errorf("inbox %s: %w", inboxID, err)
		}
		summaries = append(summaries, summary)
	}
	return summaries, rows.Err()
}
