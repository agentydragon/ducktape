package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/placement"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var sandboxActivityLease = lease.Table{
	Name: "sandbox_activity_watch", Key: []string{"sandbox_uid"}, Holder: "holder", Until: "expires_at",
}

type activityOwner struct {
	lease lease.Lease
	epoch uuid.UUID
}

func (s *Store) acquireActivity(ctx context.Context, uid string) (activityOwner, error) {
	var owner activityOwner
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `INSERT INTO sandbox_activity_watch (sandbox_uid) VALUES ($1) ON CONFLICT DO NOTHING`, uid); err != nil {
			return err
		}
		var err error
		owner.lease, err = sandboxActivityLease.Acquire(ctx, tx, []any{uid}, LeaseDuration)
		return err
	})
	return owner, err
}

func fenceActivity(ctx context.Context, tx *sql.Tx, owner activityOwner) error {
	if err := sandboxActivityLease.Fence(ctx, tx, owner.lease); err != nil {
		return err
	}
	var epoch uuid.NullUUID
	if err := tx.QueryRowContext(ctx, `SELECT stream_epoch FROM sandbox_activity_watch WHERE sandbox_uid=$1`, owner.lease.Key[0]).Scan(&epoch); err != nil {
		return err
	}
	if !epoch.Valid || epoch.UUID != owner.epoch {
		return lease.ErrLost
	}
	return nil
}

// Streams invalidate prior observations before dialing. Confirmed-down reconciliation keeps
// an identical down record, avoiding a write/notification/reconcile loop.
func (s *Store) beginActivityEpoch(ctx context.Context, owner activityOwner, invalidate bool) (activityOwner, error) {
	owner.epoch = uuid.New()
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := sandboxActivityLease.Fence(ctx, tx, owner.lease); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sandbox_activity_watch SET stream_epoch=$2 WHERE sandbox_uid=$1`, owner.lease.Key[0], owner.epoch); err != nil {
			return err
		}
		if invalidate {
			return writeHostedActivity(ctx, tx, owner, nil, dispatcherpb.SessionActivity_Unknown_STREAM_LOST, nil, true)
		}
		return nil
	})
	return owner, err
}

func (s *Store) recordHostActivity(ctx context.Context, owner activityOwner, updates map[string]*harnesshostpb.Session) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		return writeHostedActivity(ctx, tx, owner, updates, 0, nil, false)
	})
}

func (s *Store) loseActivityStream(ctx context.Context, owner activityOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		if err := writeHostedActivity(ctx, tx, owner, nil, dispatcherpb.SessionActivity_Unknown_STREAM_LOST, nil, false); err != nil {
			return err
		}
		// Late callbacks from this stream can no longer commit, even under the same lease.
		_, err := tx.ExecContext(ctx, `UPDATE sandbox_activity_watch SET stream_epoch=NULL WHERE sandbox_uid=$1`, owner.lease.Key[0])
		return err
	})
}

func (s *Store) recordEnvironmentDown(ctx context.Context, owner activityOwner, confirm func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error)) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		cause, err := confirm(ctx)
		if err != nil {
			return err
		}
		if cause == dispatcherpb.SessionActivity_EnvironmentDown_CAUSE_UNSPECIFIED {
			return errors.New("EnvironmentDown was not confirmed")
		}
		return writeHostedActivity(ctx, tx, owner, nil, 0, &cause, false)
	})
}

func writeHostedActivity(ctx context.Context, tx *sql.Tx, owner activityOwner, updates map[string]*harnesshostpb.Session, unknown dispatcherpb.SessionActivity_Unknown_Reason, down *dispatcherpb.SessionActivity_EnvironmentDown_Cause, preserveUnobserved bool) error {
	rows, err := tx.QueryContext(ctx, `SELECT session_id,spec,activity,activity_epoch::text FROM session WHERE sandbox_uid=$1 ORDER BY session_id`, owner.lease.Key[0])
	if err != nil {
		return err
	}
	type candidate struct {
		id       string
		activity []byte
		epoch    sql.NullString
	}
	var candidates []candidate
	for rows.Next() {
		var id string
		var specBytes, activity []byte
		var epoch sql.NullString
		if err := rows.Scan(&id, &specBytes, &activity, &epoch); err != nil {
			rows.Close()
			return err
		}
		spec := &sessionpb.SessionSpec{}
		if err := proto.Unmarshal(specBytes, spec); err != nil {
			rows.Close()
			return err
		}
		if placement.PlacementOf(spec) == placement.EnvironmentHosted {
			candidates = append(candidates, candidate{id, activity, epoch})
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	var now time.Time
	if err := tx.QueryRowContext(ctx, `SELECT clock_timestamp()`).Scan(&now); err != nil {
		return err
	}
	for _, row := range candidates {
		activity := &dispatcherpb.SessionActivity{RecordedAt: timestamppb.New(now)}
		switch {
		case down != nil:
			activity.Kind = &dispatcherpb.SessionActivity_EnvironmentDown_{EnvironmentDown: &dispatcherpb.SessionActivity_EnvironmentDown{Cause: *down}}
		case unknown != 0:
			if preserveUnobserved && row.activity == nil {
				continue
			}
			activity.Kind = &dispatcherpb.SessionActivity_Unknown_{Unknown: &dispatcherpb.SessionActivity_Unknown{Reason: unknown}}
		default:
			update, ok := updates[row.id]
			if !ok {
				continue
			}
			if update.GetSessionId() != row.id || update.GetEnvironmentHosted() == nil {
				return errors.New("HarnessHost Session does not match hosted placement")
			}
			activity.Kind = &dispatcherpb.SessionActivity_Observed{Observed: update}
		}
		// Idempotent invalidations do not advance versions or wake the observer itself forever.
		previous := &dispatcherpb.SessionActivity{}
		if proto.Unmarshal(row.activity, previous) == nil {
			previous.RecordedAt = activity.RecordedAt
			if proto.Equal(previous, activity) && (unknown != 0 || (down != nil && *down == dispatcherpb.SessionActivity_EnvironmentDown_DELETED) || (row.epoch.Valid && row.epoch.String == owner.epoch.String())) {
				continue
			}
		}
		data, err := proto.Marshal(activity)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE session SET activity=$2,activity_epoch=$3 WHERE session_id=$1`, row.id, data, owner.epoch); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) activitySandboxes(ctx context.Context, after int64) (map[SandboxKey]struct{}, int64, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_name,sandbox_uid::text,spec,version FROM session WHERE version>$1 ORDER BY version`, after)
	if err != nil {
		return nil, after, err
	}
	defer rows.Close()
	keys := map[SandboxKey]struct{}{}
	for rows.Next() {
		var key SandboxKey
		var data []byte
		if err := rows.Scan(&key.Name, &key.UID, &data, &after); err != nil {
			return nil, after, err
		}
		spec := &sessionpb.SessionSpec{}
		if err := proto.Unmarshal(data, spec); err != nil {
			return nil, after, err
		}
		if placement.PlacementOf(spec) == placement.EnvironmentHosted {
			keys[key] = struct{}{}
		}
	}
	return keys, after, rows.Err()
}

func (s *Store) hasHostedActivity(ctx context.Context, uid string) (bool, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT spec FROM session WHERE sandbox_uid=$1`, uid)
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var data []byte
		if err := rows.Scan(&data); err != nil {
			return false, err
		}
		spec := &sessionpb.SessionSpec{}
		if err := proto.Unmarshal(data, spec); err != nil {
			return false, err
		}
		if placement.PlacementOf(spec) == placement.EnvironmentHosted {
			return true, nil
		}
	}
	return false, rows.Err()
}
