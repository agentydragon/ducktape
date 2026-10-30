package projection

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/settledcursors"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
)

// stalledMessage is the only diagnostic a stalled projection shows its readers: backend and raw event text
// stay out of them.
const stalledMessage = "Session history projection stalled; retained events and the last verified view are unchanged."

var errCheckpointAdvanced = errors.New("projection checkpoint advanced during fetch; retry from checkpoint")

// Progress is how far a batch folded a Session, and how far its history is committed.
type Progress struct{ ThroughCursor, CommittedCursor uint64 }

// Projector folds at most PageSize entries of History per transaction into DB's fold tables.
type Projector struct {
	DB       *sql.DB
	History  *storage.Store
	Epoch    string
	PageSize int
}

// ProjectBatch resumes from the fold's checkpoint. A failed fold leaves that checkpoint unchanged, and
// history the fold cannot apply is retained as the feed's failed operational state.
func (p *Projector) ProjectBatch(ctx context.Context, threadID uuid.UUID, lease Lease) (Progress, error) {
	after, err := checkpointPosition(ctx, p.DB, threadID, p.Epoch)
	if err != nil {
		return Progress{}, err
	}
	progress, err := p.projectPrefix(ctx, threadID, lease, after)
	if err != nil && isUnfoldable(err) {
		if retained := p.recordFailure(ctx, threadID, lease, after, err); retained != nil {
			slog.WarnContext(ctx, "could not retain projection failure", "thread_id", threadID, "error", retained)
		}
	}
	return progress, err
}

// isUnfoldable reports whether err says the stored history cannot be folded, as opposed to a lost lease or
// a database failure that a retry may clear.
func isUnfoldable(err error) bool {
	var replicationError *ReplicationError
	var notUnderstood *threadfold.ObservationNotUnderstoodError
	var contractError *threadfold.ContractError
	return errors.As(err, &replicationError) || errors.As(err, &notUnderstood) || errors.As(err, &contractError) ||
		errors.Is(err, ErrInconsistentFold) || errors.Is(err, storage.ErrInvalidArgument)
}

func (p *Projector) recordFailure(ctx context.Context, threadID uuid.UUID, lease Lease, after uint64, cause error) error {
	return inTransaction(ctx, p.DB, func(tx *sql.Tx) error {
		if err := FenceLease(ctx, tx, lease, threadID); err != nil {
			return err
		}
		checkpoint, err := lockCheckpoint(ctx, tx, threadID)
		if err != nil {
			return err
		}
		if checkpoint == nil || checkpoint.throughCursor != after {
			return nil // no materialized view, or a newer batch already recovered
		}
		var errorCursor *uint64
		var replicationError *ReplicationError
		if errors.As(cause, &replicationError) {
			errorCursor = replicationError.Cursor
		}
		message := stalledMessage
		if _, err := SetOperational(ctx, tx, threadID, threadfold.StatusFailed, &message, errorCursor); err != nil {
			return err
		}
		return notify(ctx, tx, ChannelSummaries)
	})
}

func (p *Projector) projectPrefix(ctx context.Context, threadID uuid.UUID, lease Lease, after uint64) (Progress, error) {
	page, err := p.History.ReadPage(ctx, threadID, after, p.PageSize)
	if err != nil {
		return Progress{}, err
	}
	// Cursors a settlement or a cleanup removed are covered by their ranges, not missing.
	unstored := settledcursors.Unstored(page)
	expected := after
	for _, entry := range page.GetEntries() {
		expected = settledcursors.NextStored(expected, unstored)
		if entry.GetCursor() != expected || entry.GetOrigin().GetSequence() != entry.GetCursor() ||
			entry.GetOrigin().GetSourceId() == "" {
			return Progress{}, replication(nil, "invalid stored history page")
		}
	}
	// An empty page short of the committed prefix is valid only when a cleanup removed every cursor in
	// between; a fold with no entry yet has no source to carry over them.
	removedSuffix := len(page.GetEntries()) == 0 && after < page.GetLastCursor()
	if removedSuffix && (after == 0 || settledcursors.NextStored(after, unstored) <= page.GetLastCursor()) {
		return Progress{}, replication(nil, "stored history omitted entries from its committed prefix")
	}
	var progress Progress
	err = inTransaction(ctx, p.DB, func(tx *sql.Tx) error {
		// Serializes with other writes under this lease, including a delayed batch from an old owner. The
		// fence is not held over the page read.
		if err := FenceLease(ctx, tx, lease, threadID); err != nil {
			return err
		}
		current, err := lockCheckpoint(ctx, tx, threadID)
		if err != nil {
			return err
		}
		if current != nil && current.projectionEpoch != p.Epoch {
			if after != 0 {
				return errCheckpointAdvanced
			}
			if _, err := resetThreadFoldEpoch(ctx, tx, threadID, p.Epoch); err != nil {
				return err
			}
			current, err = lockCheckpoint(ctx, tx, threadID)
			if err != nil {
				return err
			}
		}
		if (current == nil && after != 0) || (current != nil &&
			(current.projectionEpoch != p.Epoch || current.throughCursor != after)) {
			return errCheckpointAdvanced
		}
		summary, err := loadSummary(ctx, tx, threadID)
		if err != nil {
			return err
		}
		if summary == nil {
			return replication(nil, "missing fold summary")
		}
		if len(page.GetEntries()) == 0 {
			progress, err = p.projectEmptyPage(ctx, tx, threadID, summary, page, after, removedSuffix)
		} else {
			progress, err = p.projectEntries(ctx, tx, threadID, summary, page)
		}
		return err
	})
	return progress, err
}

func (p *Projector) projectEmptyPage(
	ctx context.Context, tx *sql.Tx, threadID uuid.UUID, summary *summary, page *historypb.ReadSessionEventsResponse,
	after uint64, removedSuffix bool,
) (Progress, error) {
	// Admission can precede every native Event. Only the authenticated retained attachment establishes this
	// empty view; a catalog row alone proves no admission.
	initialized := false
	if after == 0 && page.GetFeedState().GetAttached() != nil {
		var err error
		if initialized, err = RecordEmptyThreadFold(ctx, tx, threadID, p.Epoch); err != nil {
			return Progress{}, err
		}
	}
	through := after
	if removedSuffix {
		through = page.GetLastCursor()
		if err := SkipRemovedSuffix(ctx, tx, threadID, through); err != nil {
			return Progress{}, err
		}
	}
	changed, err := projectLifecycle(ctx, tx, threadID, summary, page, through)
	if err != nil {
		return Progress{}, err
	}
	if changed {
		if err := summary.store(ctx, tx, threadID); err != nil {
			return Progress{}, err
		}
	}
	if initialized || changed || removedSuffix {
		if err := notify(ctx, tx, ChannelSummaries); err != nil {
			return Progress{}, err
		}
	}
	return Progress{ThroughCursor: through, CommittedCursor: page.GetLastCursor()}, nil
}

func (p *Projector) projectEntries(
	ctx context.Context, tx *sql.Tx, threadID uuid.UUID, summary *summary, page *historypb.ReadSessionEventsResponse,
) (Progress, error) {
	entries := page.GetEntries()
	sourceID := entries[0].GetOrigin().GetSourceId()
	for _, entry := range entries {
		if entry.GetOrigin().GetSourceId() != sourceID {
			return Progress{}, replication(nil, "mixed source identities in projection page")
		}
	}
	if err := RecordThreadFold(ctx, tx, threadID, sourceID, page, p.Epoch); err != nil {
		return Progress{}, err
	}
	if err := recordModelActivity(ctx, tx, threadID, entries); err != nil {
		return Progress{}, err
	}
	for _, entry := range entries {
		at := eventTime(entry.GetEvent())
		if entry.GetCursor() == 1 {
			summary.firstEventAt = sql.NullTime{Time: at, Valid: true}
		}
		if !summary.lastEventAt.Valid || at.After(summary.lastEventAt.Time) {
			summary.lastEventAt = sql.NullTime{Time: at, Valid: true}
		}
		if completed := entry.GetEvent().GetTurnCompleted(); completed != nil {
			summary.lastTurnStatus = sql.NullInt64{Int64: int64(completed.GetStatus()), Valid: true}
		}
		summary.idleSince = nextIdleSince(summary.idleSince, entry.GetEvent())
	}
	last := entries[len(entries)-1].GetCursor()
	if _, err := projectLifecycle(ctx, tx, threadID, summary, page, last); err != nil {
		return Progress{}, err
	}
	if err := summary.store(ctx, tx, threadID); err != nil {
		return Progress{}, err
	}
	if err := notify(ctx, tx, ChannelSummaries); err != nil {
		return Progress{}, err
	}
	return Progress{ThroughCursor: last, CommittedCursor: page.GetLastCursor()}, nil
}

// querier is the read side database/sql shares between a pool and a transaction.
type querier interface {
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// checkpointPosition returns the current prefix or zero when the one supported fold epoch transition needs replay.
func checkpointPosition(ctx context.Context, db querier, threadID uuid.UUID, projectionEpoch string) (uint64, error) {
	var currentEpoch string
	var through uint64
	err := db.QueryRowContext(ctx, "SELECT projection_epoch, through_cursor FROM thread_checkpoint WHERE thread_id = $1", threadID).
		Scan(&currentEpoch, &through)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	if currentEpoch == projectionEpoch {
		return through, nil
	}
	if currentEpoch == previousFoldEpoch && projectionEpoch == FoldEpoch {
		return 0, nil
	}
	return 0, replication(nil, "thread fold epoch %q cannot reset to %q", currentEpoch, projectionEpoch)
}

func inTransaction(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := f(tx); err != nil {
		return errors.Join(err, tx.Rollback())
	}
	return tx.Commit()
}

// eventTime is when an Event happened, at the microseconds the database keeps.
func eventTime(event *eventpb.Event) time.Time {
	return event.GetAt().AsTime().Truncate(time.Microsecond)
}
