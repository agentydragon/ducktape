package fanout

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.

	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
)

// DeliveryPageSize is how many deliveries one Deliveries call returns at most. A full page means
// there may be more.
const DeliveryPageSize = githubmodel.DeliveryPageSize

// Match is what decides which stored deliveries a GitHub subscription is owed.
type Match struct {
	SubscriptionID uuid.UUID
	InboxID        uuid.UUID
	AppID          int64
	// Subject is the subject followed, in the repository the subscription is bound to.
	Subject githubstate.SubjectKey
	Filters []githubmodel.EventFilter
	// Observation selection controls raw suppression for native commit events. Explicit check-run action filters
	// keep matching raw webhook actions and do not select generated check-run facts.
	ObserveCheckRuns      bool
	ObserveCommitStatuses bool
	// Fences are the grants the subscription may read under: a delivery of a repository outside them is not
	// owed, however it matches.
	Fences []githubstate.AccessFence
}

// Delivery is a stored delivery that matched, with the identity of its entry.
type Delivery struct {
	Event   sourceadapter.GitHubEvent
	Payload json.RawMessage
}

// argList collects query arguments and names each by its placeholder.
type argList []any

func (a *argList) add(value any) string {
	*a = append(*a, value)
	return "$" + strconv.Itoa(len(*a))
}

// Deliveries returns the next deliveries, by position, that the subscription is owed and has not been
// given: those after its start position, of its App, of a repository it reads under a fence, selected by its
// filters, and about its subject. A delivery is about the subject if it names it, or, for a commit event, if it is
// for a commit the subject has been seen at. A commit subject is only ever matched the second way.
func (c *Coordinator) Deliveries(ctx context.Context, match Match) ([]Delivery, error) {
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
		if match.Subject.Kind == "pull_request" {
			switch filter.Event {
			case githubmodel.EventPullRequestReview:
				selected[i] += " AND NOT d.pr_normalized"
			case githubmodel.EventPullRequest:
				if filter.Actions == nil {
					selected[i] += " AND NOT d.pr_normalized"
				} else {
					selected[i] += " AND NOT (d.pr_normalized AND d.action = d.pr_normalized_action)"
				}
			}
		}
		selected[i] += ")"
	}
	ci := make([]string, len(githubmodel.CIEvents()))
	for i, event := range githubmodel.CIEvents() {
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
	excludeNormalizedCommit := ""
	if match.Subject.Kind == "pull_request" {
		var excluded []string
		if match.ObserveCheckRuns {
			excluded = append(excluded, `(d.commit_normalized AND d.event = 'check_run')`)
		}
		if match.ObserveCommitStatuses {
			excluded = append(excluded, `(d.commit_normalized AND d.event = 'status')`)
		}
		if len(excluded) > 0 {
			excludeNormalizedCommit = "AND NOT (" + strings.Join(excluded, " OR ") + ")"
		}
	}
	// The relational natural key matches this delivery; payload expiry does not affect identity.
	rows, err := c.db.QueryContext(ctx, `
		SELECT d.app_id, d.delivery_id, d.repository_id, d.event, d.action, d.payload
		FROM github_delivery d
		WHERE d.app_id = `+args.add(match.AppID)+`
			AND (`+strings.Join(accessible, " OR ")+`)
			AND (`+strings.Join(selected, " OR ")+`)
			AND (`+about+`)
			`+excludeNormalizedCommit+`
			AND d.position > (SELECT github_start_position FROM subscription WHERE id = `+subscription+`)
			AND NOT EXISTS (
				SELECT 1 FROM subscription_match m
				JOIN entry e ON e.inbox_id = `+inbox+` AND e.cursor = m.cursor
				WHERE m.subscription_id = `+subscription+` AND e.identity_provider = 'github'
					AND e.github_app_id = d.app_id AND e.github_delivery_id = d.delivery_id
					AND e.github_repository_id = d.repository_id AND e.github_event = d.event
					AND e.github_action IS NOT DISTINCT FROM d.action)
		ORDER BY d.position LIMIT `+strconv.Itoa(DeliveryPageSize), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var deliveries []Delivery
	for rows.Next() {
		var delivery Delivery
		var event string
		var action sql.NullString
		var payload []byte
		if err := rows.Scan(&delivery.Event.AppID, &delivery.Event.DeliveryID,
			&delivery.Event.RepositoryID, &event, &action, &payload); err != nil {
			return nil, err
		}
		delivery.Event.Event = githubmodel.EventName(event)
		if action.Valid {
			delivery.Event.Action = &action.String
		}
		delivery.Payload = payload
		deliveries = append(deliveries, delivery)
	}
	return deliveries, rows.Err()
}

// Recording is the outcome of matching a GitHub subscription, to be recorded under the worker's claim.
type Recording struct {
	SubscriptionID uuid.UUID
	// Version and Generation are the subscription's when it was read. A subscription that moved on, by a
	// cancellation, a renewal or ingress since, is not recorded over.
	Version    int64
	Generation int64
	Matched    []Delivery
	// More says the page was full: the subscription is due again at once.
	More bool
	// Fences are the grants the match was read under, and Subject and SubjectGeneration the subject refresh.
	Fences            []githubstate.AccessFence
	Subject           githubstate.SubjectKey
	SubjectGeneration int64
	// RepairAt is the earliest expiry among access fences, checked again while recording.
	RepairAt time.Time
	// RetryAt is when pending work should resume after this page, if any.
	RetryAt *time.Time
}

// Record appends the matched deliveries to the claimed inbox and schedules the subscription's next
// attempt, in one transaction.
//
// Nothing is appended, and the subscription is made due at once, if a fence or the subject refresh no longer
// holds. If a grant expires while the page is written, the transaction is rolled back and the call fails with
// *githubstate.RefreshDeferredError. It fails with ErrClaimLost if the claim no longer holds, and does nothing for a
// cancelled, expired or changed subscription.
func (c *Coordinator) Record(ctx context.Context, claim inboxstore.Claim, recording Recording) error {
	return c.runner.Run(ctx, func(tx transaction.Tx) error {
		if err := githubstate.Lock(ctx, tx); err != nil {
			return err
		}
		if _, err := inboxstore.FenceClaimTx(ctx, tx, claim); err != nil {
			return err
		}
		var cancelled bool
		var version, generation int64
		var expiresAt time.Time
		if err := tx.QueryRowContext(ctx, `SELECT cancelled, version, generation, expires_at FROM subscription WHERE id = $1 FOR UPDATE`,
			recording.SubscriptionID).Scan(&cancelled, &version, &generation, &expiresAt); err != nil {
			return fmt.Errorf("subscription %s: %w", recording.SubscriptionID, err)
		}
		if cancelled || version != recording.Version || !expiresAt.After(now()) {
			return nil
		}
		subjectGeneration, err := githubstate.SubjectGenerationTx(ctx, tx, recording.Subject)
		if err != nil {
			return err
		}
		current := len(recording.Fences) > 0 && recording.RepairAt.After(now()) &&
			subjectGeneration == recording.SubjectGeneration
		if current {
			var err error
			if current, err = githubstate.FencesValidTx(ctx, tx, recording.Fences); err != nil {
				return err
			}
		}
		if !current {
			due := now()
			if err := c.inboxes.ScheduleSourceTx(ctx, tx, recording.SubscriptionID, &due); err != nil {
				return err
			}
			return postgres.Notify(ctx, tx, wakeup.WorkChannel, "")
		}
		for _, delivery := range recording.Matched {
			if err := c.append(ctx, tx, claim.ID, recording.SubscriptionID, delivery.Event, delivery.Payload); err != nil {
				return err
			}
		}
		mergeabilityCount, mergeabilityMore, err := c.recordMergeability(ctx, tx, claim.ID, recording.SubscriptionID, recording.Subject, recording.Fences)
		if err != nil {
			return err
		}
		commitCount, commitMore, err := c.recordCommitObservations(ctx, tx, claim.ID, recording.SubscriptionID, recording.Subject, recording.Fences)
		if err != nil {
			return err
		}
		prStateCount, prStateMore, err := c.recordPRState(ctx, tx, claim.ID, recording.SubscriptionID, recording.Subject, recording.Fences)
		if err != nil {
			return err
		}
		// Ingress that committed since the subscription was read must not be overwritten by an idle schedule.
		var next *time.Time
		if recording.RetryAt != nil {
			next = recording.RetryAt
		}
		if recording.More || mergeabilityMore || commitMore || prStateMore || generation != recording.Generation {
			due := now()
			next = &due
		}
		if err := c.inboxes.SourceSucceededTx(ctx, tx, recording.SubscriptionID, next, now()); err != nil {
			return err
		}
		if len(recording.Matched) > 0 || mergeabilityCount > 0 || commitCount > 0 || prStateCount > 0 {
			if err := c.inboxes.TouchInboxTx(ctx, tx, claim.ID, now()); err != nil {
				return err
			}
		}
		// Recheck every time-sensitive fence after appending and scheduling so expiry rolls the whole page back.
		if _, err := inboxstore.FenceClaimTx(ctx, tx, claim); err != nil {
			return err
		}
		var finalCancelled bool
		var finalVersion, finalGeneration int64
		var finalExpiresAt time.Time
		if err := tx.QueryRowContext(ctx, `SELECT cancelled, version, generation, expires_at FROM subscription WHERE id = $1`,
			recording.SubscriptionID).Scan(&finalCancelled, &finalVersion, &finalGeneration, &finalExpiresAt); err != nil {
			return err
		}
		if finalCancelled || finalVersion != version || !finalExpiresAt.After(now()) {
			return &githubstate.RefreshDeferredError{Until: now()}
		}
		finalSubjectGeneration, err := githubstate.SubjectGenerationTx(ctx, tx, recording.Subject)
		if err != nil {
			return err
		}
		if finalGeneration != generation || finalSubjectGeneration != subjectGeneration {
			return &githubstate.RefreshDeferredError{Until: now()}
		}
		valid, err := githubstate.FencesValidTx(ctx, tx, recording.Fences)
		if err != nil {
			return err
		}
		if !valid || !recording.RepairAt.After(now()) {
			return &githubstate.RefreshDeferredError{Until: now()}
		}
		return postgres.Notify(ctx, tx, wakeup.WorkChannel, "")
	})
}
