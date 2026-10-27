package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// GitHubDeliveryPageSize is how many deliveries one GitHubDeliveries call returns at most. A full page means
// there may be more.
const GitHubDeliveryPageSize = 128

// Filters is the event filters a source selects with: the ones it names, or its subject's defaults.
func (s GitHubSource) Filters() []EventFilter {
	if s.Events != nil {
		return s.Events
	}
	if _, ok := s.Subject.(IssueSubject); ok {
		return unfiltered(issueEvents)
	}
	filters := []EventFilter{{Event: EventStatus}, {Event: EventCheckRun, Actions: []string{"completed"}}}
	switch s.Subject.(type) {
	case PullRequestSubject:
		filters = append(filters, unfiltered(prEvents)...)
	case BranchSubject:
		filters = append(filters, unfiltered(refEvents)...)
	}
	return filters
}

func unfiltered(events []EventName) []EventFilter {
	filters := make([]EventFilter, len(events))
	for i, event := range events {
		filters[i] = EventFilter{Event: event}
	}
	return filters
}

// GitHubMatch is what decides which stored deliveries a GitHub subscription is owed.
type GitHubMatch struct {
	SubscriptionID uuid.UUID
	InboxID        uuid.UUID
	AppID          int64
	// Subject is the subject followed, in the repository the subscription is bound to.
	Subject GitHubSubjectKey
	Filters []EventFilter
	// Fences are the grants the subscription may read under: a delivery of a repository outside them is not
	// owed, however it matches.
	Fences []GitHubAccessFence
}

// GitHubDelivery is a stored delivery that matched, with the identity of its entry.
type GitHubDelivery struct {
	Position int64
	Event    GitHubEvent
	Payload  json.RawMessage
}

// argList collects query arguments and names each by its placeholder.
type argList []any

func (a *argList) add(value any) string {
	*a = append(*a, value)
	return "$" + strconv.Itoa(len(*a))
}

// GitHubDeliveries returns the next deliveries, by position, that the subscription is owed and has not been
// given: those after its start position, of its App, of a repository it reads under a fence, selected by its
// filters, and about its subject. A delivery is about the subject if it names it, or, for a CI event, if it is
// for a commit the subject has been seen at. A commit subject is only ever matched the second way.
func (s *Store) GitHubDeliveries(ctx context.Context, match GitHubMatch) ([]GitHubDelivery, error) {
	if len(match.Filters) == 0 || len(match.Fences) == 0 {
		return nil, errors.New("a GitHub match needs filters and fences")
	}
	var args argList
	accessible := make([]string, len(match.Fences))
	for i, fence := range match.Fences {
		accessible[i] = fmt.Sprintf("(d.repository_id = %s AND d.installation_id = %s)",
			args.add(fence.Key.RepositoryID), args.add(fence.Key.InstallationID))
	}
	selected := make([]string, len(match.Filters))
	for i, filter := range match.Filters {
		selected[i] = "(d.event = " + args.add(string(filter.Event))
		if filter.Actions != nil {
			actions := make([]string, len(filter.Actions))
			for j, action := range filter.Actions {
				actions[j] = args.add(action)
			}
			selected[i] += " AND d.action IN (" + strings.Join(actions, ", ") + ")"
		}
		selected[i] += ")"
	}
	ci := make([]string, len(ciEvents))
	for i, event := range ciEvents {
		ci[i] = args.add(string(event))
	}
	repository, kind, key := args.add(match.Subject.RepositoryID), args.add(match.Subject.Kind), args.add(match.Subject.Key)
	about := "(d.event IN (" + strings.Join(ci, ", ") + `) AND EXISTS (
			SELECT 1 FROM github_subject_revision r
			WHERE r.repository_id = ` + repository + ` AND r.kind = ` + kind + ` AND r.subject_key = ` + key + `
				AND r.head_repository_id = d.repository_id AND r.sha = d.head_sha))`
	if match.Subject.Kind != "commit" {
		about = `EXISTS (
			SELECT 1 FROM github_delivery_subject ds
			WHERE ds.delivery_position = d.position AND ds.repository_id = ` + repository + ` AND ds.kind = ` + kind + `
				AND ds.subject_key = ` + key + `) OR ` + about
	}
	subscription, inbox := args.add(match.SubscriptionID), args.add(match.InboxID)
	// The whole identity is compared, so the inbox/event unique index serves the lookup.
	rows, err := s.db.QueryContext(ctx, `
		SELECT d.position, d.app_id, d.delivery_id, d.repository_id, d.event, d.action, d.payload
		FROM github_delivery d
		WHERE d.app_id = `+args.add(match.AppID)+`
			AND (`+strings.Join(accessible, " OR ")+`)
			AND (`+strings.Join(selected, " OR ")+`)
			AND (`+about+`)
			AND d.position > (SELECT github_start_position FROM subscription WHERE id = `+subscription+`)
			AND NOT EXISTS (
				SELECT 1 FROM subscription_match m
				JOIN entry e ON e.inbox_id = `+inbox+` AND e.cursor = m.cursor
				WHERE m.subscription_id = `+subscription+` AND e.event = jsonb_build_object(
					'provider', 'github', 'app_id', d.app_id, 'delivery_id', d.delivery_id,
					'repository_id', d.repository_id, 'event', d.event, 'action', d.action))
		ORDER BY d.position LIMIT `+strconv.Itoa(GitHubDeliveryPageSize), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var deliveries []GitHubDelivery
	for rows.Next() {
		var delivery GitHubDelivery
		var event string
		var action sql.NullString
		var payload []byte
		if err := rows.Scan(&delivery.Position, &delivery.Event.AppID, &delivery.Event.DeliveryID,
			&delivery.Event.RepositoryID, &event, &action, &payload); err != nil {
			return nil, err
		}
		delivery.Event.Event = EventName(event)
		if action.Valid {
			delivery.Event.Action = &action.String
		}
		delivery.Payload = payload
		deliveries = append(deliveries, delivery)
	}
	return deliveries, rows.Err()
}

// GitHubRecording is the outcome of matching a GitHub subscription, to be recorded under the worker's claim.
type GitHubRecording struct {
	SubscriptionID uuid.UUID
	// Version and Generation are the subscription's when it was read. A subscription that moved on, by a
	// cancellation, a renewal or ingress since, is not recorded over.
	Version    int64
	Generation int64
	Matched    []GitHubDelivery
	// More says the page was full: the subscription is due again at once.
	More bool
	// Fences are the grants the match was read under, and Subject and SubjectGeneration the subject refresh.
	Fences            []GitHubAccessFence
	Subject           GitHubSubjectKey
	SubjectGeneration int64
	// RepairAt is when the match must be made again: the earliest a grant, or the subject's refresh, goes stale.
	RepairAt time.Time
}

// RecordGitHub appends the matched deliveries to the claimed inbox and schedules the subscription's next
// attempt, in one transaction.
//
// Nothing is appended, and the subscription is made due at once, if a fence or the subject refresh no longer
// holds. If a grant expires while the page is written, the transaction is rolled back and the call fails with
// *RefreshDeferredError. It fails with ErrClaimLost if the claim no longer holds, and does nothing for a
// cancelled, expired or changed subscription.
func (s *Store) RecordGitHub(ctx context.Context, claim Claim, recording GitHubRecording) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		if _, err := fenced(ctx, tx, claim); err != nil {
			return err
		}
		var cancelled bool
		var version, generation int64
		var expiresAt time.Time
		if err := tx.QueryRowContext(ctx, `SELECT cancelled, version, generation, expires_at FROM subscription WHERE id = $1`,
			recording.SubscriptionID).Scan(&cancelled, &version, &generation, &expiresAt); err != nil {
			return fmt.Errorf("subscription %s: %w", recording.SubscriptionID, err)
		}
		if cancelled || version != recording.Version || !expiresAt.After(now()) {
			return nil
		}
		var subjectGeneration int64
		table, where, args := recording.Subject.row()
		if err := tx.QueryRowContext(ctx, "SELECT generation FROM "+table+" WHERE "+where, args[:]...).Scan(&subjectGeneration); err != nil {
			return fmt.Errorf("%s %+v: %w", table, recording.Subject, err)
		}
		current := len(recording.Fences) > 0 && recording.RepairAt.After(now()) &&
			subjectGeneration == recording.SubjectGeneration
		if current {
			var err error
			if current, err = fencesValid(ctx, tx, recording.Fences); err != nil {
				return err
			}
		}
		if !current {
			if _, err := tx.ExecContext(ctx, `UPDATE subscription SET next_attempt = $2 WHERE id = $1`,
				recording.SubscriptionID, now()); err != nil {
				return err
			}
			return notify(ctx, tx)
		}
		for _, delivery := range recording.Matched {
			if err := s.AppendEvent(ctx, tx, claim.ID, recording.SubscriptionID, delivery.Event, delivery.Payload); err != nil {
				return err
			}
		}
		mergeabilityCount, mergeabilityMore, err := s.recordMergeability(ctx, tx, claim.ID, recording)
		if err != nil {
			return err
		}
		// Ingress that committed since the subscription was read must not be overwritten by an idle schedule.
		next := recording.RepairAt
		if recording.More || mergeabilityMore || generation != recording.Generation {
			next = now()
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE subscription SET next_attempt = $2, last_success_at = $3, error_kind = NULL, error = NULL,
				error_since = NULL, error_observed_at = NULL
			WHERE id = $1`, recording.SubscriptionID, next, now()); err != nil {
			return err
		}
		if len(recording.Matched) > 0 || mergeabilityCount > 0 {
			if _, err := tx.ExecContext(ctx, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, claim.ID, now()); err != nil {
				return err
			}
		}
		// Appending took time: roll the whole page back if a grant expired meanwhile.
		valid, err := fencesValid(ctx, tx, recording.Fences)
		if err != nil {
			return err
		}
		if !valid || !recording.RepairAt.After(now()) {
			return &RefreshDeferredError{Until: now()}
		}
		return notify(ctx, tx)
	})
}
