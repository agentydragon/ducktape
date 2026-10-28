package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

const (
	// MaxNoticeStateSessions is the most Sessions one SessionNoticeStates reads.
	MaxNoticeStateSessions = 200
	// maxInboxesPerSandbox is the most inboxes SandboxStatus reports for one Sandbox.
	maxInboxesPerSandbox = 1000
	// maxPendingEntrySummaries is the most entries SandboxStatus describes for one inbox.
	maxPendingEntrySummaries = 100
	// maxSummaryField is the longest an event's state, action or conclusion is shown, in characters.
	maxSummaryField = 80
)

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

// NoticeProgress is how far an inbox's latest notice got.
type NoticeProgress string

const (
	NoticePrepared  NoticeProgress = "prepared"
	NoticeAttempted NoticeProgress = "attempted"
	NoticeAdmitted  NoticeProgress = "admitted"
	NoticeConfirmed NoticeProgress = "confirmed"
)

func (n Notice) Progress() NoticeProgress {
	switch {
	case n.Confirmed:
		return NoticeConfirmed
	case n.Admitted:
		return NoticeAdmitted
	case n.Attempted:
		return NoticeAttempted
	}
	return NoticePrepared
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
func entriesBeyond(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, after int64) (*EntryTimes, int64, error) {
	var first, last sql.NullTime
	var count int64
	if err := tx.QueryRowContext(ctx,
		`SELECT min(created_at), max(created_at), count(*) FROM entry WHERE inbox_id = $1 AND cursor > $2`,
		inboxID, after).Scan(&first, &last, &count); err != nil || count == 0 {
		return nil, 0, err
	}
	return &EntryTimes{First: first.Time.UTC(), Last: last.Time.UTC()}, count, nil
}

// EntryProvider is the kind of source an entry came from.
type EntryProvider string

const (
	EntryFromActions EntryProvider = "actions"
	EntryFromGitHub  EntryProvider = "github"
	EntryFromMessage EntryProvider = "message"
)

// EntrySummary describes an entry the agent has not acknowledged without its payload.
type EntrySummary struct {
	Cursor    int64
	CreatedAt time.Time
	Provider  EntryProvider
	Summary   string
}

// summarize describes an entry from its event identity and the few lifecycle fields of its payload that are
// safe to show: never the content a source delivered.
func summarize(cursor int64, event model.EventIdentity, payload []byte, createdAt time.Time) (EntrySummary, error) {
	var fields map[string]any
	if err := json.Unmarshal(payload, &fields); err != nil {
		return EntrySummary{}, fmt.Errorf("entry %d payload: %w", cursor, err)
	}
	summary := EntrySummary{Cursor: cursor, CreatedAt: createdAt.UTC()}
	switch event := event.(type) {
	case model.ActionsEvent:
		summary.Provider = EntryFromActions
		summary.Summary = fmt.Sprintf("Action %s · event %d", event.RequestID, event.Sequence)
		// Action state is a provider-owned lifecycle value, not arbitrary result content.
		if state, ok := fields["state"].(string); ok {
			summary.Summary += " · " + truncate(state)
		}
	case model.GitHubEvent:
		summary.Provider = EntryFromGitHub
		summary.Summary = "GitHub " + string(event.Event)
		if event.Action != nil && *event.Action != "" {
			summary.Summary += " · " + truncate(*event.Action)
		}
		if event.Event == model.EventCheckRun || event.Event == model.EventWorkflowRun {
			if check, ok := fields[string(event.Event)].(map[string]any); ok {
				if conclusion, ok := check["conclusion"].(string); ok {
					summary.Summary += " · " + truncate(conclusion)
				}
			}
		}
	case model.GitHubObservationEvent:
		summary.Provider = EntryFromGitHub
		summary.Summary = fmt.Sprintf("GitHub %s observation · sequence %d", event.Event, event.Sequence)
	case model.GitHubMergeabilityEvent:
		summary.Provider = EntryFromGitHub
		summary.Summary = fmt.Sprintf("GitHub PR #%d mergeability · sequence %d", event.PullRequestNumber, event.Sequence)
	case model.GitHubMergeabilitySnapshotEvent:
		summary.Provider = EntryFromGitHub
		summary.Summary = fmt.Sprintf("GitHub PR #%d mergeability snapshot · version %d", event.PullRequestNumber, event.Version)
	case model.MessageEvent:
		// Who sent it is what the service authenticated; the body is never shown.
		summary.Provider = EntryFromMessage
		summary.Summary = fmt.Sprintf("Message from %s/%s", event.Sender.Namespace, event.Sender.Name)
	default:
		return EntrySummary{}, fmt.Errorf("entry %d: unknown event identity %T", cursor, event)
	}
	return summary, nil
}

func truncate(s string) string {
	if characters := []rune(s); len(characters) > maxSummaryField {
		return string(characters[:maxSummaryField])
	}
	return s
}

// SubscriptionStatus is a subscription as the operator sees it.
type SubscriptionStatus struct {
	Subscription
	// NextSourceCheckAt is when the source is next read, whatever the subscription's state; nil if never.
	NextSourceCheckAt *time.Time
}

// InboxStatus is one inbox of a Sandbox, with what holds its next notice.
type InboxStatus struct {
	Inbox         Inbox
	Notice        *Notice
	Subscriptions []SubscriptionStatus
	// UnannouncedCount is the entries no notice covers yet, and PendingAcknowledgementCount those the agent has
	// not acknowledged, covered or not.
	UnannouncedCount            int64
	PendingAcknowledgementCount int64
	// PendingEntries are the oldest entries awaiting acknowledgement, at most 100; more are pending when
	// PendingAcknowledgementCount is greater than their number.
	PendingEntries []EntrySummary
	NoticeWait     NoticeWait
	// NextWorkAt is when a worker next claims the inbox, nil for a retired inbox.
	NextWorkAt *time.Time
}

// SandboxNotificationStatus is the inboxes that name one incarnation of a Sandbox.
type SandboxNotificationStatus struct {
	ObservedAt time.Time
	Inboxes    []InboxStatus
}

// SandboxStatus reads the inboxes whose destination is exactly `ref`, retired ones included, at most 1000 in
// Session order. A Sandbox with the same name and another UID has none. Reading changes nothing.
func (s *Store) SandboxStatus(ctx context.Context, ref model.DestinationRef, debounce Debounce) (SandboxNotificationStatus, error) {
	var status SandboxNotificationStatus
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		status.ObservedAt = now()
		inboxes, nextWork, err := inboxesOf(ctx, tx, `
			WHERE destination_ref->>'namespace' = $1 AND destination_ref->>'name' = $2 AND destination_ref->>'uid' = $3
			ORDER BY session_id, id LIMIT $4`, ref.Namespace, ref.Name, ref.UID, maxInboxesPerSandbox)
		if err != nil {
			return err
		}
		for i, inbox := range inboxes {
			inboxStatus := InboxStatus{Inbox: inbox, NextWorkAt: nextWork[i]}
			if inboxStatus.Subscriptions, err = subscriptionStatuses(ctx, tx, inbox.ID, status.ObservedAt); err != nil {
				return err
			}
			if inboxStatus.Notice, err = latestNotice(ctx, tx, inbox.ID); err != nil {
				return err
			}
			unannounced, unannouncedCount, err := entriesBeyond(ctx, tx, inbox.ID, max(inbox.Covered, inbox.Acknowledged, inbox.ExpiredThrough))
			if err != nil {
				return err
			}
			inboxStatus.UnannouncedCount = unannouncedCount
			inboxStatus.NoticeWait = noticeWait(inbox, inboxStatus.Notice, unannounced, status.ObservedAt, debounce)
			awaiting := max(inbox.Acknowledged, inbox.ExpiredThrough)
			if _, inboxStatus.PendingAcknowledgementCount, err = entriesBeyond(ctx, tx, inbox.ID, awaiting); err != nil {
				return err
			}
			if inboxStatus.PendingEntries, err = pendingEntrySummaries(ctx, tx, inbox.ID, awaiting); err != nil {
				return err
			}
			status.Inboxes = append(status.Inboxes, inboxStatus)
		}
		return nil
	})
	return status, err
}

// inboxesOf reads the inboxes a WHERE and ORDER BY clause selects, with each one's next_attempt.
func inboxesOf(ctx context.Context, tx *sql.Tx, clause string, args ...any) ([]Inbox, []*time.Time, error) {
	rows, err := tx.QueryContext(ctx, `SELECT `+inboxColumns+`, next_attempt FROM inbox `+clause, args...)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var inboxes []Inbox
	var nextWork []*time.Time
	for rows.Next() {
		var next sql.NullTime
		inbox, err := scanInbox(rows, &next)
		if err != nil {
			return nil, nil, err
		}
		inboxes = append(inboxes, inbox)
		nextWork = append(nextWork, nil)
		if next.Valid {
			nextWork[len(nextWork)-1] = utc(next.Time)
		}
	}
	return inboxes, nextWork, rows.Err()
}

func subscriptionStatuses(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, observed time.Time) ([]SubscriptionStatus, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT `+subscriptionColumns+`, s.next_attempt FROM subscription s WHERE s.inbox_id = $1 ORDER BY s.id`, inboxID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var statuses []SubscriptionStatus
	for rows.Next() {
		var next sql.NullTime
		subscription, err := scanSubscription(rows, observed, &next)
		if err != nil {
			return nil, err
		}
		status := SubscriptionStatus{Subscription: subscription}
		if next.Valid {
			status.NextSourceCheckAt = utc(next.Time)
		}
		statuses = append(statuses, status)
	}
	return statuses, rows.Err()
}

func pendingEntrySummaries(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, after int64) ([]EntrySummary, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, event, payload, created_at FROM entry WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inboxID, after, maxPendingEntrySummaries)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var summaries []EntrySummary
	for rows.Next() {
		var cursor int64
		var event, payload []byte
		var createdAt time.Time
		if err := rows.Scan(&cursor, &event, &payload, &createdAt); err != nil {
			return nil, err
		}
		identity, err := model.DecodeEventIdentity(event)
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inboxID, cursor, err)
		}
		summary, err := summarize(cursor, identity, payload, createdAt)
		if err != nil {
			return nil, fmt.Errorf("inbox %s: %w", inboxID, err)
		}
		summaries = append(summaries, summary)
	}
	return summaries, rows.Err()
}

// InboxNoticeState is one live inbox at a glance: what waits for a notice, what a notice covers that the agent
// has not acknowledged, and how far the latest notice got.
type InboxNoticeState struct {
	InboxID   uuid.UUID
	SessionID string
	// Queued is the entries no notice covers yet.
	Queued int64
	// Unacknowledged is the entries a notice covers that the agent has not acknowledged.
	Unacknowledged int64
	// Notice is how far the latest notice got, nil before the first.
	Notice *NoticeProgress
	// NoticeWait carries, while debouncing, when a notice of the queued entries becomes due.
	NoticeWait NoticeWait
	// Error is the latest notice's error, or else the inbox's delivery error.
	Error *string
}

// SessionNoticeStates are the live inboxes of the named Sessions.
type SessionNoticeStates struct {
	ObservedAt time.Time
	Inboxes    []InboxNoticeState
}

// SessionNoticeStates reads the live inboxes of the named Sessions in `namespace`, whoever owns them, in
// Session order. Reading changes nothing: only an agent's acknowledgement moves what counts as acknowledged.
// More than MaxNoticeStateSessions distinct Sessions fail with model.ErrInvalid.
func (s *Store) SessionNoticeStates(ctx context.Context, namespace string, sessionIDs []string, debounce Debounce) (SessionNoticeStates, error) {
	// Never nil, so that it is a JSON array whatever the caller passes.
	distinct := append([]string{}, sessionIDs...)
	slices.Sort(distinct)
	distinct = slices.Compact(distinct)
	if len(distinct) > MaxNoticeStateSessions {
		return SessionNoticeStates{}, model.Invalid("at most %d Sessions per read", MaxNoticeStateSessions)
	}
	wanted, err := json.Marshal(distinct)
	if err != nil {
		return SessionNoticeStates{}, err
	}
	var states SessionNoticeStates
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		states.ObservedAt = now()
		inboxes, _, err := inboxesOf(ctx, tx, `
			WHERE destination_ref->>'namespace' = $1 AND NOT retired
				AND session_id IN (SELECT jsonb_array_elements_text($2::jsonb))
			ORDER BY session_id, id`, namespace, string(wanted))
		if err != nil {
			return err
		}
		for _, inbox := range inboxes {
			notice, err := latestNotice(ctx, tx, inbox.ID)
			if err != nil {
				return err
			}
			queued, queuedCount, err := entriesBeyond(ctx, tx, inbox.ID, max(inbox.Covered, inbox.Acknowledged, inbox.ExpiredThrough))
			if err != nil {
				return err
			}
			_, unacknowledgedCount, err := entriesBeyond(ctx, tx, inbox.ID, max(inbox.Acknowledged, inbox.ExpiredThrough))
			if err != nil {
				return err
			}
			state := InboxNoticeState{
				InboxID: inbox.ID, SessionID: inbox.SessionID, Queued: queuedCount, Unacknowledged: unacknowledgedCount - queuedCount,
				NoticeWait: noticeWait(inbox, notice, queued, states.ObservedAt, debounce), Error: inbox.DeliveryError,
			}
			if notice != nil {
				progress := notice.Progress()
				state.Notice = &progress
				if notice.Error != nil {
					state.Error = notice.Error
				}
			}
			states.Inboxes = append(states.Inboxes, state)
		}
		return nil
	})
	return states, err
}
