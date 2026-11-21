package store

import (
	"bytes"
	"context"
	"database/sql"
	"strings"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestDispatchVersionsCaptureOldWriterUpdatesAndDeletions(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 4)
	commandID := "test-version-retained-receipt"
	const sessionID = "test-version-retained-session"
	const sandboxName = "test-version-private-sandbox-name"
	const sandboxUID = "00000000-0000-4000-8000-000000000091"
	_, err := enqueueWithSession(t, store, ctx, sessionID, sandboxName, sandboxUID, input(commandID, "test-version-private-command-body"), nil)
	if err != nil {
		t.Fatal(err)
	}
	initialVersion := dispatchVersion(t, store.DB, commandID)
	if initialVersion <= 0 {
		t.Fatalf("inserted dispatch version = %d, want positive", initialVersion)
	}

	owner := claimSessionOwner(t, store, sessionID)
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_DENIED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
		ObservedAt: timestamppb.Now(),
	}
	blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED}
	if err := store.SetDeliveryAssessment(ctx, commandID, owner, false, blocker, assessment); err != nil {
		t.Fatal("store wake evidence:", err)
	}
	admission := &eventlogpb.EventEntry{Cursor: 37}
	if _, err := store.Settle(ctx, commandID, owner, []string{QueueState}, AdmittedState, "", admission); err != nil {
		t.Fatal("settle admitted receipt:", err)
	}
	beforeLegacyUpdate := dispatchVersion(t, store.DB, commandID)
	if _, err := store.DB.ExecContext(ctx, `UPDATE dispatch SET reason='test-version-old-writer-update'
		WHERE command_id=$1`, commandID); err != nil {
		t.Fatal("old-shaped update:", err)
	}
	beforeDelete := dispatchVersion(t, store.DB, commandID)
	if beforeDelete <= beforeLegacyUpdate {
		t.Fatalf("old-shaped SQL UPDATE version = %d, prior version = %d", beforeDelete, beforeLegacyUpdate)
	}

	rollback, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer rollback.Rollback()
	if _, err := rollback.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id=$1", commandID); err != nil {
		rollback.Rollback()
		t.Fatal("delete inside rollback transaction:", err)
	}
	if count := tombstoneCount(t, rollback, commandID); count != 1 {
		rollback.Rollback()
		t.Fatalf("tombstones visible in delete transaction = %d, want 1", count)
	}
	if err := rollback.Rollback(); err != nil {
		t.Fatal("rollback delete:", err)
	}
	if _, err := store.GetDispatch(ctx, commandID); err != nil {
		t.Fatalf("dispatch after delete rollback: %v", err)
	}
	if count := tombstoneCount(t, store.DB, commandID); count != 0 {
		t.Fatalf("tombstones after delete rollback = %d, want 0", count)
	}

	if _, err := store.DB.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id=$1", commandID); err != nil {
		t.Fatal("old-shaped delete:", err)
	}
	var tombstoneVersion int64
	var tombstoneSessionID, operation, state string
	var tombstoneReason sql.NullString
	var tombstoneAdmission, tombstoneBlocker, tombstoneWake []byte
	if err := store.DB.QueryRowContext(ctx, `SELECT version,session_id,operation,state,reason,admission,delivery_blocker,wake_assessment
		FROM dispatch_tombstone WHERE command_id=$1`, commandID).Scan(
		&tombstoneVersion, &tombstoneSessionID, &operation, &state, &tombstoneReason,
		&tombstoneAdmission, &tombstoneBlocker, &tombstoneWake,
	); err != nil {
		t.Fatal("read tombstone:", err)
	}
	if tombstoneVersion <= beforeDelete || tombstoneSessionID != sessionID || operation != "submit_input" || state != AdmittedState || !tombstoneReason.Valid || tombstoneReason.String != "test-version-old-writer-update" {
		t.Fatalf("tombstone identity/state = version %d, session %q, operation %q, state %q, reason %q", tombstoneVersion, tombstoneSessionID, operation, state, tombstoneReason.String)
	}
	if !bytes.Equal(tombstoneAdmission, mustWire(t, admission)) || !bytes.Equal(tombstoneBlocker, mustWire(t, blocker)) || !bytes.Equal(tombstoneWake, mustWire(t, assessment)) {
		t.Fatalf("tombstone evidence differs: admission=%x blocker=%x wake=%x", tombstoneAdmission, tombstoneBlocker, tombstoneWake)
	}
	var serialized string
	if err := store.DB.QueryRowContext(ctx, "SELECT to_jsonb(t)::text FROM dispatch_tombstone t WHERE command_id=$1", commandID).Scan(&serialized); err != nil {
		t.Fatal("serialize tombstone:", err)
	}
	for _, privateValue := range []string{sandboxName, sandboxUID} {
		if strings.Contains(serialized, privateValue) {
			t.Fatalf("tombstone exposed Sandbox address %q: %s", privateValue, serialized)
		}
	}
	for _, addressOrPayloadColumn := range []string{`"sandbox_name"`, `"sandbox_uid"`, `"command"`} {
		if strings.Contains(serialized, addressOrPayloadColumn) {
			t.Fatalf("tombstone contains address or command field %q: %s", addressOrPayloadColumn, serialized)
		}
	}

	_, err = enqueueWithSession(t, store, ctx, sessionID, sandboxName, sandboxUID, input(commandID, "test-version-private-command-body"), nil)
	if err != nil {
		t.Fatal("reuse deleted command ID:", err)
	}
	secondVersion := dispatchVersion(t, store.DB, commandID)
	if secondVersion <= tombstoneVersion {
		t.Fatalf("reused dispatch version = %d, prior tombstone version = %d", secondVersion, tombstoneVersion)
	}
	if _, err := store.DB.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id=$1", commandID); err != nil {
		t.Fatal("delete reused command ID:", err)
	}
	var tombstoneVersions []int64
	rows, err := store.DB.QueryContext(ctx, "SELECT version FROM dispatch_tombstone WHERE command_id=$1 ORDER BY version", commandID)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var version int64
		if err := rows.Scan(&version); err != nil {
			rows.Close()
			t.Fatal(err)
		}
		tombstoneVersions = append(tombstoneVersions, version)
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	if len(tombstoneVersions) != 2 || tombstoneVersions[0] >= tombstoneVersions[1] {
		t.Fatalf("reused command tombstone versions = %v, want two increasing versions", tombstoneVersions)
	}
}

func TestDispatchTombstonePruningAdvancesDurableCoverageFloor(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 4)
	firstCommand := "test-version-prune-lower-expired"
	secondCommand := "test-version-prune-higher-expired"
	recentCommand := "test-version-prune-recent"
	for _, commandID := range []string{firstCommand, secondCommand, recentCommand} {
		if _, err := enqueueWithSession(t, store, ctx, "test-version-prune-session", "test-version-prune-sandbox", "00000000-0000-4000-8000-000000000092", input(commandID, "test-version-prune-payload"), nil); err != nil {
			t.Fatal(err)
		}
		if _, err := store.DB.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id=$1", commandID); err != nil {
			t.Fatal(err)
		}
	}
	lowerVersion := tombstoneVersion(t, store.DB, firstCommand)
	higherVersion := tombstoneVersion(t, store.DB, secondCommand)
	for _, commandID := range []string{firstCommand, secondCommand} {
		if _, err := store.DB.ExecContext(ctx, `UPDATE dispatch_tombstone SET deleted_at=clock_timestamp() - interval '48 hours'
			WHERE command_id=$1`, commandID); err != nil {
			t.Fatal("backdate expired test tombstone:", err)
		}
	}
	lockedLower, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer lockedLower.Rollback()
	var lockedCommand string
	if err := lockedLower.QueryRowContext(ctx, `SELECT command_id FROM dispatch_tombstone
		WHERE command_id=$1 FOR UPDATE`, firstCommand).Scan(&lockedCommand); err != nil {
		t.Fatal("lock lower expired tombstone:", err)
	}

	pruned, err := store.PruneDispatchTombstones(ctx)
	if err != nil || pruned != 1 {
		t.Fatalf("prune unlocked expired tombstone: count=%d err=%v", pruned, err)
	}
	if count := tombstoneCount(t, store.DB, firstCommand); count != 1 {
		t.Fatalf("locked expired tombstone count = %d, want 1", count)
	}
	if count := tombstoneCount(t, store.DB, secondCommand); count != 0 {
		t.Fatalf("unlocked expired tombstone count = %d, want 0", count)
	}
	if count := tombstoneCount(t, store.DB, recentCommand); count != 1 {
		t.Fatalf("recent tombstone count = %d, want 1", count)
	}
	var floor int64
	if err := store.DB.QueryRowContext(ctx, "SELECT pruned_through FROM dispatch_watch_retention WHERE singleton").Scan(&floor); err != nil {
		t.Fatal(err)
	}
	if floor != higherVersion {
		t.Fatalf("pruned_through = %d, want unlocked deleted version %d", floor, higherVersion)
	}
	if higherVersion <= lowerVersion {
		t.Fatalf("sequence versions = %d then %d, want lower version first", lowerVersion, higherVersion)
	}
	if err := lockedLower.Commit(); err != nil {
		t.Fatal("release locked tombstone:", err)
	}
	pruned, err = store.PruneDispatchTombstones(ctx)
	if err != nil || pruned != 1 {
		t.Fatalf("prune previously locked expired tombstone: count=%d err=%v", pruned, err)
	}
	if count := tombstoneCount(t, store.DB, firstCommand); count != 0 {
		t.Fatalf("expired lower tombstone count = %d, want 0", count)
	}
	if err := store.DB.QueryRowContext(ctx, "SELECT pruned_through FROM dispatch_watch_retention WHERE singleton").Scan(&floor); err != nil {
		t.Fatal(err)
	}
	if floor != higherVersion {
		t.Fatalf("coverage floor regressed to %d after deleting older version %d", floor, lowerVersion)
	}

	restarted := NewStore(store.DB, 4)
	pruned, err = restarted.PruneDispatchTombstones(ctx)
	if err != nil || pruned != 0 {
		t.Fatalf("prune with restarted Store: count=%d err=%v", pruned, err)
	}
	var restartedFloor int64
	if err := restarted.DB.QueryRowContext(ctx, "SELECT pruned_through FROM dispatch_watch_retention WHERE singleton").Scan(&restartedFloor); err != nil {
		t.Fatal(err)
	}
	if restartedFloor != floor {
		t.Fatalf("coverage floor after Store restart = %d, want %d", restartedFloor, floor)
	}
}

func TestDispatchTombstonePruningRequiresCoverageRow(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 2)
	commandID := "test-version-prune-missing-floor"
	if _, err := enqueueWithSession(t, store, ctx, "test-version-prune-missing-floor-session", "test-version-prune-missing-floor-sandbox", "00000000-0000-4000-8000-000000000093", input(commandID, "test-version-payload"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id=$1", commandID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE dispatch_tombstone SET deleted_at=clock_timestamp() - interval '48 hours'
		WHERE command_id=$1`, commandID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "DELETE FROM dispatch_watch_retention WHERE singleton"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.PruneDispatchTombstones(ctx); err == nil {
		t.Fatal("prune succeeded without its durable coverage row")
	}
	if count := tombstoneCount(t, store.DB, commandID); count != 1 {
		t.Fatalf("tombstone count after failed prune = %d, want 1", count)
	}
}

func TestDispatchVersionCheckpointWaitsForEarlierSequenceWriter(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	store := testStore(t, 2)
	txFirst, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer txFirst.Rollback()
	firstPID := postgres.BackendPID(t, ctx, txFirst)
	firstVersion := insertVersionTestDispatch(t, ctx, txFirst, "test-version-order-first")

	txSecond, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer txSecond.Rollback()
	var secondVersion int64
	if err := txSecond.QueryRowContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at)
		VALUES ('test-version-order-second','test-version-order-session','test-version-order-sandbox',
		'00000000-0000-4000-8000-000000000094','submit_input',decode('0801','hex'),'QUEUED_IN_DISPATCHER',now(),now())
		RETURNING version`).Scan(&secondVersion); err != nil {
		txSecond.Rollback()
		t.Fatal("insert later sequence writer:", err)
	}
	if err := txSecond.Commit(); err != nil {
		t.Fatal("commit later sequence writer:", err)
	}
	if firstVersion >= secondVersion {
		t.Fatalf("sequence versions = %d then %d, want allocation order before reverse commit order", firstVersion, secondVersion)
	}

	var naiveCheckpoint int64
	if err := store.DB.QueryRowContext(ctx, "SELECT max(version) FROM dispatch").Scan(&naiveCheckpoint); err != nil {
		t.Fatal("read naive checkpoint:", err)
	}
	if naiveCheckpoint != secondVersion {
		t.Fatalf("naive checkpoint = %d, want visible committed maximum %d", naiveCheckpoint, secondVersion)
	}
	if rowsAtOrBelowVersion(t, ctx, store.DB, naiveCheckpoint) != 1 {
		t.Fatal("uncommitted lower version was visible to the naive checkpoint")
	}

	checkpointTx, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer checkpointTx.Rollback()
	checkpointPID := postgres.BackendPID(t, ctx, checkpointTx)
	locked := make(chan error, 1)
	go func() {
		_, err := checkpointTx.ExecContext(ctx, "LOCK TABLE dispatch, dispatch_tombstone IN SHARE MODE")
		locked <- err
	}()
	postgres.WaitForBlocker(t, ctx, store.DB, checkpointPID, firstPID)
	if err := txFirst.Commit(); err != nil {
		t.Fatal("commit earlier sequence writer:", err)
	}
	if err := <-locked; err != nil {
		t.Fatal("acquire stable checkpoint table locks:", err)
	}
	var stableCheckpoint int64
	if err := checkpointTx.QueryRowContext(ctx, `SELECT GREATEST(
		COALESCE((SELECT max(version) FROM dispatch),0),
		COALESCE((SELECT max(version) FROM dispatch_tombstone),0))`).Scan(&stableCheckpoint); err != nil {
		t.Fatal("read stable checkpoint:", err)
	}
	if stableCheckpoint != secondVersion || rowsAtOrBelowVersion(t, ctx, checkpointTx, stableCheckpoint) != 2 {
		t.Fatalf("stable checkpoint %d covers %d rows, want both versions through %d", stableCheckpoint, rowsAtOrBelowVersion(t, ctx, checkpointTx, stableCheckpoint), secondVersion)
	}
}

func dispatchVersion(t *testing.T, db interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, commandID string,
) int64 {
	t.Helper()
	var version int64
	if err := db.QueryRowContext(t.Context(), "SELECT version FROM dispatch WHERE command_id=$1", commandID).Scan(&version); err != nil {
		t.Fatal(err)
	}
	return version
}

func tombstoneVersion(t *testing.T, db interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, commandID string,
) int64 {
	t.Helper()
	var version int64
	if err := db.QueryRowContext(t.Context(), "SELECT version FROM dispatch_tombstone WHERE command_id=$1", commandID).Scan(&version); err != nil {
		t.Fatal(err)
	}
	return version
}

func tombstoneCount(t *testing.T, db interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, commandID string,
) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM dispatch_tombstone WHERE command_id=$1", commandID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func insertVersionTestDispatch(t *testing.T, ctx context.Context, tx *sql.Tx, commandID string) int64 {
	t.Helper()
	var version int64
	err := tx.QueryRowContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at)
		VALUES ($1,'test-version-order-session','test-version-order-sandbox',
		'00000000-0000-4000-8000-000000000094','submit_input',decode('0801','hex'),'QUEUED_IN_DISPATCHER',now(),now())
		RETURNING version`, commandID).Scan(&version)
	if err != nil {
		t.Fatal("insert ordered test dispatch:", err)
	}
	return version
}

func rowsAtOrBelowVersion(t *testing.T, ctx context.Context, db interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, version int64,
) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(ctx, "SELECT count(*) FROM dispatch WHERE version <= $1", version).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func mustWire(t *testing.T, message proto.Message) []byte {
	t.Helper()
	data, err := proto.Marshal(message)
	if err != nil {
		t.Fatal(err)
	}
	return data
}
