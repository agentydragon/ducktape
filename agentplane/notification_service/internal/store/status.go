package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	entrystorage "github.com/agentydragon/ducktape/agentplane/notification_service/internal/store/internal/entry"

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

// EntrySummary describes an entry the agent has not acknowledged without its payload.
type EntrySummary struct {
	Cursor           int64
	CreatedAt        time.Time
	Event            model.EventIdentity
	ActionState      *string
	GitHubConclusion *string
}

// summarize describes an entry from its event identity and the few lifecycle fields of its payload that are
// safe to show: never the content a source delivered.
func summarize(cursor int64, event model.EventIdentity, payload []byte, createdAt time.Time) (EntrySummary, error) {
	var fields map[string]any
	if err := json.Unmarshal(payload, &fields); err != nil {
		return EntrySummary{}, fmt.Errorf("entry %d payload: %w", cursor, err)
	}
	summary := EntrySummary{Cursor: cursor, CreatedAt: createdAt.UTC(), Event: event}
	switch event := event.(type) {
	case model.ActionsEvent:
		if state, ok := fields["state"].(string); ok {
			summary.ActionState = &state
		}
	case model.GitHubEvent:
		if event.Event == model.EventCheckRun || event.Event == model.EventWorkflowRun {
			if check, ok := fields[string(event.Event)].(map[string]any); ok {
				if conclusion, ok := check["conclusion"].(string); ok {
					switch conclusion {
					case "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success", "timed_out", "startup_failure":
						summary.GitHubConclusion = &conclusion
					}
				}
			}
		}
	case model.GitHubObservationEvent, model.GitHubMergeabilityEvent, model.GitHubMergeabilitySnapshotEvent, model.MessageEvent:
	default:
		return EntrySummary{}, fmt.Errorf("entry %d: unknown event identity %T", cursor, event)
	}
	return summary, nil
}

func summarizeStored(cursor int64, event model.EventIdentity, githubPayload []byte, content storagecodec.EntryContentStorage, createdAt time.Time) (EntrySummary, error) {
	if _, ok := event.(model.MessageEvent); ok {
		return EntrySummary{Cursor: cursor, CreatedAt: createdAt.UTC(), Event: event}, nil
	}
	payload, err := storagecodec.EntryPayloadFromRead(event, githubPayload, content)
	if err != nil {
		return EntrySummary{}, fmt.Errorf("entry %d content: %w", cursor, err)
	}
	return summarize(cursor, event, payload, createdAt)
}

// Status needs only Action and mergeability lifecycle fields. Message summaries use the authenticated sender in
// the identity, so the SQL projection never fetches or decodes a Message body.
const entryContentSummaryReadColumns = `action_state, action_at,
	action_actor_kind, action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject, action_actor_identity_name, action_actor_identity_uid,
	mergeability_repository, mergeability_base_ref, mergeability_head_sha, mergeability_base_sha,
	mergeability_previous, mergeability_current, mergeability_observed_at, mergeability_observed_at_submicro_nanos,
	merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha, NULL::text, NULL::text`

// InboxStatus is one inbox of a Sandbox, with what holds its next notice.
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
	PendingEntries []EntrySummary
	NoticeWait     NoticeWait
	// NextWorkAt is when a worker next claims the inbox, nil for a retired inbox.
	NextWorkAt *time.Time
}

// SessionStatus reads the one inbox for a Session. A workload only sees its own current Sandbox
// incarnation; the operator passes a nil owner. Missing and unauthorized inboxes both return nil.
func (s *Store) SessionStatus(ctx context.Context, namespace, sessionID string, owner Owner, debounce Debounce, entries, subscriptions bool) (*InboxStatus, time.Time, error) {
	observed := now()
	var result *InboxStatus
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		clause := `WHERE destination_namespace = $1 AND session_id = $2`
		args := []any{namespace, sessionID}
		if owner != nil {
			predicate, ownerArgs := ownerPredicate(owner, "", 3)
			clause += ` AND ` + predicate
			args = append(args, ownerArgs...)
		}
		inboxes, nextWork, err := inboxesOf(ctx, tx, clause, args...)
		if err != nil || len(inboxes) == 0 {
			return err
		}
		if len(inboxes) != 1 {
			return fmt.Errorf("multiple inboxes for Session")
		}
		value, err := readInboxStatus(ctx, tx, inboxes[0], nextWork[0], observed, debounce, entries, subscriptions)
		if err != nil {
			return err
		}
		result = &value
		return nil
	})
	return result, observed, err
}

func readInboxStatus(ctx context.Context, tx *sql.Tx, inbox Inbox, nextWork *time.Time, observed time.Time, debounce Debounce, entries, subscriptions bool) (InboxStatus, error) {
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

func subscriptionStatuses(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, observed time.Time) ([]Subscription, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT `+subscriptionColumns+` FROM subscription s WHERE s.inbox_id = $1 ORDER BY s.id`, inboxID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var statuses []Subscription
	for rows.Next() {
		subscription, err := scanSubscription(rows, observed)
		if err != nil {
			return nil, err
		}
		statuses = append(statuses, subscription)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	for i := range statuses {
		if err := loadGitHubStatus(ctx, tx, &statuses[i]); err != nil {
			return nil, err
		}
	}
	return statuses, nil
}

func pendingEntrySummaries(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, after int64) ([]EntrySummary, error) {
	rows, err := tx.QueryContext(ctx,
		`SELECT cursor, `+entrystorage.ReadColumns+`,
			CASE WHEN identity_provider IN ('github', 'github_observation') THEN payload END, `+entryContentSummaryReadColumns+`, created_at FROM entry
			WHERE inbox_id = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`,
		inboxID, after, maxPendingEntrySummaries)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var summaries []EntrySummary
	for rows.Next() {
		var cursor int64
		var identityRead entrystorage.IdentityRow
		var payload []byte
		var content storagecodec.EntryContentStorage
		var createdAt time.Time
		destinations := append([]any{&cursor}, identityRead.Destinations()...)
		destinations = append(destinations, &payload)
		destinations = append(destinations, content.Destinations()...)
		destinations = append(destinations, &createdAt)
		if err := rows.Scan(destinations...); err != nil {
			return nil, err
		}
		identity, err := identityRead.Event()
		if err != nil {
			return nil, fmt.Errorf("inbox %s entry %d event: %w", inboxID, cursor, err)
		}
		summary, err := summarizeStored(cursor, identity, payload, content, createdAt)
		if err != nil {
			return nil, fmt.Errorf("inbox %s: %w", inboxID, err)
		}
		summaries = append(summaries, summary)
	}
	return summaries, rows.Err()
}
