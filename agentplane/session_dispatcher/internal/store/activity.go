package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var sandboxActivityLease = lease.Table{
	Name: "sandbox_activity_watch", Key: []string{"sandbox_uid"}, Holder: "holder", Until: "expires_at",
}

var samplerActivityLease = lease.Table{
	Name: "sandbox_activity_watch", Key: []string{"central_sampler"}, Holder: "holder", Until: "expires_at",
}

type ActivitySandbox struct{ Name, UID string }

type ActivityOwner struct {
	lease   lease.Lease
	Epoch   uuid.UUID
	Central bool
}

func (o ActivityOwner) table() lease.Table {
	if o.Central {
		return samplerActivityLease
	}
	return sandboxActivityLease
}

func (o ActivityOwner) Matches(session *harnesshostpb.Session) bool {
	if o.Central {
		return session.GetCentralSamplerV1() != nil
	}
	return session.GetEnvironmentHosted() != nil
}

func (s *Store) AcquireSamplerActivity(ctx context.Context) (ActivityOwner, error) {
	owner := ActivityOwner{Central: true}
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `INSERT INTO sandbox_activity_watch (central_sampler) VALUES (true) ON CONFLICT DO NOTHING`); err != nil {
			return err
		}
		var err error
		owner.lease, err = owner.table().Acquire(ctx, tx, []any{true}, LeaseDuration)
		return err
	})
	return owner, err
}

func (s *Store) AcquireActivity(ctx context.Context, uid string) (ActivityOwner, error) {
	var owner ActivityOwner
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

func fenceActivity(ctx context.Context, tx *sql.Tx, owner ActivityOwner) error {
	if err := owner.table().Fence(ctx, tx, owner.lease); err != nil {
		return err
	}
	var epoch uuid.NullUUID
	if err := tx.QueryRowContext(ctx, `SELECT stream_epoch FROM sandbox_activity_watch WHERE holder=$1`, owner.lease.Holder).Scan(&epoch); err != nil {
		return err
	}
	if !epoch.Valid || epoch.UUID != owner.Epoch {
		return lease.ErrLost
	}
	return nil
}

// Streams invalidate prior observations before dialing. Confirmed-down reconciliation keeps
// an identical down record, avoiding a write/notification/reconcile loop.
func (s *Store) BeginActivityEpoch(ctx context.Context, owner ActivityOwner, invalidate bool) (ActivityOwner, error) {
	owner.Epoch = uuid.New()
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := owner.table().Fence(ctx, tx, owner.lease); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sandbox_activity_watch SET stream_epoch=$2 WHERE holder=$1`, owner.lease.Holder, owner.Epoch); err != nil {
			return err
		}
		if invalidate {
			return writeHostActivity(ctx, tx, owner, nil, dispatcherpb.SessionActivity_Unknown_STREAM_LOST, nil, true)
		}
		return nil
	})
	return owner, err
}

func (s *Store) RecordHostActivity(ctx context.Context, owner ActivityOwner, updates map[string]*harnesshostpb.Session) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		return writeHostActivity(ctx, tx, owner, updates, 0, nil, false)
	})
}

func (s *Store) LoseActivityStream(ctx context.Context, owner ActivityOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		if err := writeHostActivity(ctx, tx, owner, nil, dispatcherpb.SessionActivity_Unknown_STREAM_LOST, nil, false); err != nil {
			return err
		}
		// Late callbacks from this stream can no longer commit, even under the same lease.
		_, err := tx.ExecContext(ctx, `UPDATE sandbox_activity_watch SET stream_epoch=NULL WHERE holder=$1`, owner.lease.Holder)
		return err
	})
}

func (s *Store) RecordEnvironmentDown(ctx context.Context, owner ActivityOwner, confirm func(context.Context) (dispatcherpb.SessionActivity_EnvironmentDown_Cause, error)) error {
	if owner.Central {
		return errors.New("central Sessions cannot be EnvironmentDown")
	}
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
		return writeHostActivity(ctx, tx, owner, nil, 0, &cause, false)
	})
}

func writeHostActivity(ctx context.Context, tx *sql.Tx, owner ActivityOwner, updates map[string]*harnesshostpb.Session, unknown dispatcherpb.SessionActivity_Unknown_Reason, down *dispatcherpb.SessionActivity_EnvironmentDown_Cause, preserveUnobserved bool) error {
	var uid any
	if !owner.Central {
		uid = owner.lease.Key[0]
	}
	rows, err := tx.QueryContext(ctx, `SELECT session_id,spec,activity,activity_epoch::text FROM session WHERE $1 OR sandbox_uid=$2 ORDER BY session_id`, owner.Central, uid)
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
		wanted := placement.EnvironmentHosted
		if owner.Central {
			wanted = placement.CentralSampler
		}
		if placement.PlacementOf(spec) == wanted {
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
			if update.GetSessionId() != row.id || !owner.Matches(update) {
				return errors.New("HarnessHost Session does not match placement")
			}
			activity.Kind = &dispatcherpb.SessionActivity_Observed{Observed: update}
		}
		// Idempotent invalidations do not advance versions or wake the observer itself forever.
		previous := &dispatcherpb.SessionActivity{}
		if proto.Unmarshal(row.activity, previous) == nil {
			previous.RecordedAt = activity.RecordedAt
			if proto.Equal(previous, activity) && (unknown != 0 || (down != nil && *down == dispatcherpb.SessionActivity_EnvironmentDown_DELETED) || (row.epoch.Valid && row.epoch.String == owner.Epoch.String())) {
				continue
			}
		}
		data, err := proto.Marshal(activity)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE session SET activity=$2,activity_epoch=$3 WHERE session_id=$1`, row.id, data, owner.Epoch); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) ActivitySandboxes(ctx context.Context, after int64) (map[ActivitySandbox]struct{}, int64, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_name,sandbox_uid::text,spec,version FROM session WHERE version>$1 ORDER BY version`, after)
	if err != nil {
		return nil, after, err
	}
	defer rows.Close()
	keys := map[ActivitySandbox]struct{}{}
	for rows.Next() {
		var key ActivitySandbox
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

func (s *Store) HasHostedActivity(ctx context.Context, uid string) (bool, error) {
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

func (s *Store) ReleaseActivity(ctx context.Context, owner ActivityOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error { return owner.table().Release(ctx, tx, owner.lease) })
}

func (s *Store) RenewActivity(ctx context.Context, owner ActivityOwner) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := fenceActivity(ctx, tx, owner); err != nil {
			return err
		}
		_, err := owner.table().Renew(ctx, tx, owner.lease, LeaseDuration)
		return err
	})
}
