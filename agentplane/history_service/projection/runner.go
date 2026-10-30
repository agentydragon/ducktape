package projection

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"runtime/debug"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/google/uuid"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

const (
	// FoldEpoch is on every fold row, so a reader holding rows of another epoch sees a scope reset.
	// The app's tests read rows by it: keep `FOLD_EPOCH` in `fold/models.py` equal.
	FoldEpoch         = "history-v2"
	previousFoldEpoch = "history-v1"
	// LeaseDuration is how long a Session's lease outlives the cycle that took or renewed it.
	LeaseDuration = 30 * time.Second
	// FoldHealthService is the `grpc.health.v1` service whose status says whether this replica's fold is
	// running. Probes check the whole server (""), so a stopped fold leaves raw reads served.
	FoldHealthService = "agentplane.history_service.Fold"
	// leaseReleaseTimeout bounds handing leases back when the fold stops.
	leaseReleaseTimeout = 5 * time.Second
)

// Backlog is how far a cycle left the retained Sessions from their committed history.
type Backlog struct {
	Sessions, Behind int
	EventsBehind     uint64
}

// bugError is a panic in the fold itself, which stops it rather than retrying.
type bugError struct {
	value any
	stack []byte
}

func (e *bugError) Error() string {
	return fmt.Sprintf("panic in the thread fold: %v\n%s", e.value, e.stack)
}

// FoldRunner folds every retained Session in FoldEpoch, resuming from its `thread_checkpoint` row (cursor 0
// before its first fold): each cycle advances every Session by at most one page, at most `concurrency` at a
// time, under per-Session leases, so replicas share the work and live Sessions keep up.
type FoldRunner struct {
	db          *sql.DB
	projector   *Projector
	concurrency int

	mu     sync.Mutex
	leases map[uuid.UUID]Lease
}

// NewFoldRunner folds `history`'s Sessions into the fold tables of `db`, which holds both and is the only
// place the fold writes.
func NewFoldRunner(db *sql.DB, history *storage.Store, concurrency, pageSize int) *FoldRunner {
	return &FoldRunner{
		db:          db,
		projector:   &Projector{DB: db, History: history, Epoch: FoldEpoch, PageSize: pageSize},
		concurrency: concurrency,
		leases:      map[uuid.UUID]Lease{},
	}
}

// Run folds until ctx ends, reporting FoldHealthService SERVING while it does, then hands every lease back so
// another replica need not wait out LeaseDuration. A failure it does not retry stops the fold, not the
// process, so raw reads stay served: it is logged and FoldHealthService turns NOT_SERVING.
//
// A cycle that finds a Session behind runs again at once; otherwise it waits for `changes`, a signal that
// history committed, or for `interval`.
func (r *FoldRunner) Run(ctx context.Context, interval time.Duration, changes <-chan struct{}, status *health.Server) {
	status.SetServingStatus(FoldHealthService, healthpb.HealthCheckResponse_SERVING)
	defer r.releaseAll(context.WithoutCancel(ctx))
	for {
		// A commit during the cycle leaves its signal pending, so the next wait ends at once.
		select {
		case <-changes:
		default:
		}
		backlog, err := r.Cycle(ctx)
		var bug *bugError
		switch {
		case ctx.Err() != nil:
			return
		case errors.As(err, &bug):
			slog.ErrorContext(ctx, "Thread fold stopped", "error", err)
			status.SetServingStatus(FoldHealthService, healthpb.HealthCheckResponse_NOT_SERVING)
			return
		case err != nil:
			slog.WarnContext(ctx, "Thread fold cycle failed; will retry", "error", err)
		default:
			slog.InfoContext(ctx, "Thread fold", "sessions", backlog.Sessions, "behind", backlog.Behind,
				"events_behind", backlog.EventsBehind)
			if backlog.Behind > 0 {
				continue
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-changes:
		case <-time.After(interval):
		}
	}
}

// Cycle advances every retained Session by at most one page.
func (r *FoldRunner) Cycle(ctx context.Context) (Backlog, error) {
	sessionIDs, err := r.discover(ctx)
	if err != nil {
		return Backlog{}, err
	}
	retained := make(map[uuid.UUID]bool, len(sessionIDs))
	for _, sessionID := range sessionIDs {
		retained[sessionID] = true
	}
	for _, sessionID := range r.heldLeases() {
		if !retained[sessionID] {
			if err := r.release(ctx, sessionID); err != nil {
				return Backlog{}, err
			}
		}
	}
	progress := make([]*Progress, len(sessionIDs))
	var group errgroup.Group
	group.SetLimit(r.concurrency)
	for index, sessionID := range sessionIDs {
		group.Go(func() (err error) {
			defer func() {
				if value := recover(); value != nil {
					err = &bugError{value: value, stack: debug.Stack()}
				}
			}()
			progress[index] = r.advance(ctx, sessionID)
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return Backlog{}, err
	}
	backlog := Backlog{Sessions: len(sessionIDs)}
	for _, advanced := range progress {
		if advanced != nil && advanced.ThroughCursor < advanced.CommittedCursor {
			backlog.Behind++
			backlog.EventsBehind += advanced.CommittedCursor - advanced.ThroughCursor
		}
	}
	return backlog, nil
}

// discover is every retained Session, including those of deleted Sandboxes, each with a summary row to fold into.
func (r *FoldRunner) discover(ctx context.Context) ([]uuid.UUID, error) {
	var sessionIDs []uuid.UUID
	err := inTransaction(ctx, r.db, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `WITH retained AS (SELECT id FROM session_history),
			summarized AS (INSERT INTO thread_history_summary (thread_id) SELECT id FROM retained ON CONFLICT DO NOTHING)
			SELECT id FROM retained`)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var sessionID uuid.UUID
			if err := rows.Scan(&sessionID); err != nil {
				return err
			}
			sessionIDs = append(sessionIDs, sessionID)
		}
		return rows.Err()
	})
	return sessionIDs, err
}

// advance folds one page of the Session, or nil when this replica did not: another replica holds the Session,
// or its history or the database failed. One Session's unfoldable history or lost lease or connection must
// not stop the others; its checkpoint is retained and the next cycle retries it.
func (r *FoldRunner) advance(ctx context.Context, sessionID uuid.UUID) *Progress {
	lease, err := r.lease(ctx, sessionID)
	if err == nil && lease != nil {
		var progress Progress
		if progress, err = r.projector.ProjectBatch(ctx, sessionID, *lease); err == nil {
			slog.DebugContext(ctx, "Thread fold", "session_id", sessionID, "through", progress.ThroughCursor,
				"committed", progress.CommittedCursor)
			return &progress
		}
	}
	if err != nil && ctx.Err() == nil {
		slog.WarnContext(ctx, "Thread fold stalled; will retry", "session_id", sessionID, "error", err)
	}
	return nil
}

func (r *FoldRunner) heldLeases() []uuid.UUID {
	r.mu.Lock()
	defer r.mu.Unlock()
	held := make([]uuid.UUID, 0, len(r.leases))
	for sessionID := range r.leases {
		held = append(held, sessionID)
	}
	return held
}

// lease renews the lease this replica holds on the Session, or takes it; nil when another replica holds it.
func (r *FoldRunner) lease(ctx context.Context, sessionID uuid.UUID) (*Lease, error) {
	var lease *Lease
	err := inTransaction(ctx, r.db, func(tx *sql.Tx) error {
		r.mu.Lock()
		held, found := r.leases[sessionID]
		r.mu.Unlock()
		if found {
			renewed, err := RenewLease(ctx, tx, held, LeaseDuration)
			if err != nil {
				return err
			}
			if renewed {
				lease = &held
				return nil
			}
			r.mu.Lock()
			delete(r.leases, sessionID)
			r.mu.Unlock()
		}
		var err error
		lease, err = AcquireLease(ctx, tx, sessionID, LeaseDuration)
		return err
	})
	if err != nil || lease == nil {
		return nil, err
	}
	r.mu.Lock()
	r.leases[sessionID] = *lease
	r.mu.Unlock()
	return lease, nil
}

func (r *FoldRunner) release(ctx context.Context, sessionID uuid.UUID) error {
	r.mu.Lock()
	lease, found := r.leases[sessionID]
	delete(r.leases, sessionID)
	r.mu.Unlock()
	if !found {
		return nil
	}
	return inTransaction(ctx, r.db, func(tx *sql.Tx) error { return ReleaseLease(ctx, tx, lease) })
}

func (r *FoldRunner) releaseAll(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, leaseReleaseTimeout)
	defer cancel()
	for _, sessionID := range r.heldLeases() {
		if err := r.release(ctx, sessionID); err != nil {
			slog.WarnContext(ctx, "could not hand back a projection lease", "session_id", sessionID, "error", err)
		}
	}
}
