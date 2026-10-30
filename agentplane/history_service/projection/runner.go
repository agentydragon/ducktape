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
	"github.com/lib/pq"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

const (
	// FoldEpoch is on every fold row, so a reader holding rows of another epoch sees a scope reset.
	// The app's tests read rows by it: keep `FOLD_EPOCH` in `fold/models.py` equal. A new epoch also needs a
	// migration marking every Session pending in `session_fold_pending`, or only Sessions that change refold.
	FoldEpoch = "history-v1"
	// LeaseDuration is how long a Session's lease outlives the cycle that took or renewed it.
	LeaseDuration = 30 * time.Second
	// FoldHealthService is the `grpc.health.v1` service whose status says whether this replica's fold is
	// running. Probes check the whole server (""), so a stopped fold leaves raw reads served.
	FoldHealthService = "agentplane.history_service.Fold"
	// leaseReleaseTimeout bounds handing leases back when the fold stops.
	leaseReleaseTimeout = 5 * time.Second
)

// Backlog is how far a cycle left the Sessions it claimed from their committed history.
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

// claim is a pending Session this replica leased for a cycle, with the version of its pending mark.
type claim struct {
	lease   Lease
	version int64
}

// FoldRunner folds the pending Sessions in FoldEpoch, resuming each from its `thread_checkpoint` row (cursor 0
// before its first fold). A `session_history` trigger marks a Session pending in `session_fold_pending` when
// it is created or its committed prefix or feed state changes. Each cycle advances every pending Session it
// can lease by at most one page, at most `concurrency` at a time, so replicas share the work and live
// Sessions keep up; a Session leaves the queue, and its lease is handed back, once a fold catches it up.
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

// Cycle advances every pending Session this replica can lease by at most one page.
func (r *FoldRunner) Cycle(ctx context.Context) (Backlog, error) {
	claims, err := r.claim(ctx)
	if err != nil {
		return Backlog{}, err
	}
	claimed := make(map[uuid.UUID]bool, len(claims))
	for _, c := range claims {
		claimed[c.lease.SessionID] = true
	}
	// A lease on a Session no longer pending, or no longer retained, guards nothing.
	for _, sessionID := range r.heldLeases() {
		if !claimed[sessionID] {
			if err := r.release(ctx, sessionID); err != nil {
				return Backlog{}, err
			}
		}
	}
	progress := make([]*Progress, len(claims))
	var group errgroup.Group
	group.SetLimit(r.concurrency)
	for index, c := range claims {
		group.Go(func() (err error) {
			defer func() {
				if value := recover(); value != nil {
					err = &bugError{value: value, stack: debug.Stack()}
				}
			}()
			progress[index] = r.advance(ctx, c)
			return nil
		})
	}
	if err := group.Wait(); err != nil {
		return Backlog{}, err
	}
	backlog := Backlog{Sessions: len(claims)}
	for _, advanced := range progress {
		if advanced != nil && advanced.ThroughCursor < advanced.CommittedCursor {
			backlog.Behind++
			backlog.EventsBehind += advanced.CommittedCursor - advanced.ThroughCursor
		}
	}
	return backlog, nil
}

// claim leases every pending Session no other replica holds, renewing the leases this replica holds, and gives
// each a summary row to fold into. `SKIP LOCKED` keeps concurrent claims disjoint without waiting on each
// other; the lease keeps other replicas off a Session until this one catches it up.
func (r *FoldRunner) claim(ctx context.Context) ([]claim, error) {
	r.mu.Lock()
	held := make(map[uuid.UUID]Lease, len(r.leases))
	tokens := make([]string, 0, len(r.leases))
	for sessionID, lease := range r.leases {
		held[sessionID] = lease
		tokens = append(tokens, lease.Token.String())
	}
	r.mu.Unlock()
	var claims []claim
	err := inTransaction(ctx, r.db, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT pending.session_id, pending.version FROM session_fold_pending pending
			WHERE NOT EXISTS (SELECT 1 FROM session_projection_lease lease WHERE lease.session_id = pending.session_id
				AND lease.expires_at > clock_timestamp() AND lease.token <> ALL($1::uuid[]))
			FOR UPDATE OF pending SKIP LOCKED`, pq.Array(tokens))
		if err != nil {
			return err
		}
		var pending []claim
		for rows.Next() {
			var c claim
			if err := rows.Scan(&c.lease.SessionID, &c.version); err != nil {
				return errors.Join(err, rows.Close())
			}
			pending = append(pending, c)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return err
		}
		claims = nil
		sessionIDs := make([]string, 0, len(pending))
		for _, c := range pending {
			lease, err := renewOrAcquire(ctx, tx, held, c.lease.SessionID)
			if err != nil {
				return err
			}
			if lease != nil {
				c.lease = *lease
				claims = append(claims, c)
				sessionIDs = append(sessionIDs, lease.SessionID.String())
			}
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO thread_history_summary (thread_id) SELECT unnest($1::uuid[])
			ON CONFLICT DO NOTHING`, pq.Array(sessionIDs))
		return err
	})
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	for _, c := range claims {
		r.leases[c.lease.SessionID] = c.lease
	}
	r.mu.Unlock()
	return claims, nil
}

// renewOrAcquire renews the lease this replica holds on the Session, or takes it; nil when another replica
// holds it.
func renewOrAcquire(ctx context.Context, tx *sql.Tx, held map[uuid.UUID]Lease, sessionID uuid.UUID) (*Lease, error) {
	if lease, found := held[sessionID]; found {
		renewed, err := RenewLease(ctx, tx, lease, LeaseDuration)
		if err != nil || renewed {
			return &lease, err
		}
	}
	return AcquireLease(ctx, tx, sessionID, LeaseDuration)
}

// advance folds one page of the claimed Session, or returns nil when its history or the database failed. One
// Session's unfoldable history or lost lease or connection must not stop the others; it stays pending and
// the next cycle retries it.
func (r *FoldRunner) advance(ctx context.Context, c claim) *Progress {
	sessionID := c.lease.SessionID
	progress, err := r.projector.ProjectBatch(ctx, sessionID, c.lease)
	if err == nil {
		slog.DebugContext(ctx, "Thread fold", "session_id", sessionID, "through", progress.ThroughCursor,
			"committed", progress.CommittedCursor)
		if progress.ThroughCursor < progress.CommittedCursor {
			return &progress
		}
		if err = r.caughtUp(ctx, c); err == nil {
			return &progress
		}
	}
	if ctx.Err() == nil {
		slog.WarnContext(ctx, "Thread fold stalled; will retry", "session_id", sessionID, "error", err)
	}
	return nil
}

// caughtUp takes the Session off the queue and hands back its lease, unless it was marked again since the
// claim: the page the fold read after the claim covers every change marked before it. A later mark has a new
// version, and one in flight holds the pending row, so the delete waits for it and then finds the new version.
func (r *FoldRunner) caughtUp(ctx context.Context, c claim) error {
	var settled bool
	err := inTransaction(ctx, r.db, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, "DELETE FROM session_fold_pending WHERE session_id = $1 AND version = $2",
			c.lease.SessionID, c.version)
		if err != nil {
			return err
		}
		deleted, err := result.RowsAffected()
		if err != nil || deleted == 0 {
			return err
		}
		settled = true
		return ReleaseLease(ctx, tx, c.lease)
	})
	if err == nil && settled {
		r.mu.Lock()
		delete(r.leases, c.lease.SessionID)
		r.mu.Unlock()
	}
	return err
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
