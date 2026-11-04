package migrations

import (
	"bytes"
	"context"
	"database/sql"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	_ "github.com/lib/pq"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestSessionPlacementUpgradePreservesRetainedCentralOwner(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 10); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	for _, test := range []struct {
		id, want string
		harness  sessionpb.Harness
	}{
		{"retained-native", "environment_hosted", sessionpb.Harness_HARNESS_CLAUDE},
		{"retained-central", "central_sampler", sessionpb.Harness_HARNESS_CENTRAL_SAMPLER},
	} {
		if err := insertCurrentSessionWriterRow(t.Context(), db, test.id); err != nil {
			t.Fatal(err)
		}
		data := protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), uint64(test.harness))
		data = protowire.AppendBytes(protowire.AppendTag(data, 99, protowire.BytesType), []byte("unrelated"))
		if _, err := db.ExecContext(t.Context(), "UPDATE session_opening SET spec=$2 WHERE session_id=$1", test.id, data); err != nil {
			t.Fatal(err)
		}
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct{ id, want string }{{"retained-native", "environment_hosted"}, {"retained-central", "central_sampler"}} {
		var raw []byte
		if err := db.QueryRowContext(t.Context(), "SELECT spec FROM session WHERE session_id=$1", test.id).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		var spec sessionpb.SessionSpec
		if err := proto.Unmarshal(raw, &spec); err != nil {
			t.Fatal(err)
		}
		got := "environment_hosted"
		if spec.GetPlacement().GetCentralSamplerV1() != nil {
			got = "central_sampler"
		}
		var target *sessionpb.ExactEnvironment
		if got == "environment_hosted" {
			target = spec.GetPlacement().GetEnvironmentHosted().GetEnvironment()
		} else {
			target = spec.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
		}
		if got != test.want || target.GetName() != "sandbox" || target.GetUid() != "00000000-0000-0000-0000-000000000001" ||
			!bytes.Equal(spec.ProtoReflect().GetUnknown(), protowire.AppendBytes(protowire.AppendTag(nil, 99, protowire.BytesType), []byte("unrelated"))) {
			t.Fatalf("%s placement = %v, want %q with exact target", test.id, spec.GetPlacement(), test.want)
		}
	}
}

func TestSessionResourceUpgradeRetainsMaterializationEvidence(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 10); err != nil {
		t.Fatal(err)
	}
	states := []struct{ before, after string }{
		{"QUEUED_IN_DISPATCHER", "WAITING"},
		{"DELIVERING_TO_RUNNER", "IN_PROGRESS"},
		{"RUNNER_ADMITTED", "ACCEPTED"},
		{"FAILED", "FAILED"},
		{"UNKNOWN", "UNKNOWN"},
	}
	blocker := []byte{0x08, 0x01}
	wake := []byte{0x08, 0x02}
	for _, state := range states {
		id := "retained-" + state.after
		if err := insertCurrentSessionWriterRow(t.Context(), db, id); err != nil {
			t.Fatal(err)
		}
		legacyCentral := protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), uint64(sessionpb.Harness_HARNESS_CENTRAL_SAMPLER))
		if _, err := db.ExecContext(t.Context(), `UPDATE session_opening SET state=$2,reason=$3,spec=$4,delivery_blocker=$5,wake_assessment=$6
			WHERE session_id=$1`, id, state.before, "retained "+state.before, legacyCentral, blocker, wake); err != nil {
			t.Fatal(err)
		}
	}
	admission := []byte{0x08, 0x01}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,reason,admission,created_at,updated_at)
		VALUES ('uncertain-command','retained-UNKNOWN','sandbox','00000000-0000-0000-0000-000000000001',
		'submit_input',$1,'UNKNOWN','effect_unconfirmed',$2,now(),now())`, []byte{}, admission); err != nil {
		t.Fatal(err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	for _, state := range states {
		id := "retained-" + state.after
		var materialization, reason string
		var gotBlocker, gotWake, raw []byte
		if err := db.QueryRowContext(t.Context(), `SELECT state,reason,delivery_blocker,wake_assessment,spec FROM session WHERE session_id=$1`, id).
			Scan(&materialization, &reason, &gotBlocker, &gotWake, &raw); err != nil {
			t.Fatal(err)
		}
		var spec sessionpb.SessionSpec
		if err := proto.Unmarshal(raw, &spec); err != nil {
			t.Fatal(err)
		}
		if materialization != state.after || reason != "retained "+state.before || !bytes.Equal(gotBlocker, blocker) || !bytes.Equal(gotWake, wake) ||
			spec.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != "00000000-0000-0000-0000-000000000001" {
			t.Fatalf("%s after upgrade: state=%q reason=%q blocker=%x wake=%x placement=%v", id, materialization, reason, gotBlocker, gotWake, spec.GetPlacement())
		}
	}
	var commandState, commandReason string
	var gotAdmission []byte
	if err := db.QueryRowContext(t.Context(), `SELECT state,reason,admission FROM dispatch WHERE command_id='uncertain-command'`).
		Scan(&commandState, &commandReason, &gotAdmission); err != nil {
		t.Fatal(err)
	}
	if commandState != "UNKNOWN" || commandReason != "effect_unconfirmed" || !bytes.Equal(gotAdmission, admission) {
		t.Fatalf("uncertain command changed during Session migration: state=%q reason=%q admission=%x", commandState, commandReason, gotAdmission)
	}
}

func TestUpgradeFromVersionTwoDropsStopDispatches(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 2); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	for _, row := range []struct {
		commandID string
		operation string
	}{
		{commandID: "stop-1", operation: "stop_runner_session"},
		{commandID: "send-1", operation: "submit_input"},
	} {
		_, err := db.ExecContext(t.Context(), `
			INSERT INTO dispatch (
				command_id, session_id, sandbox_name, sandbox_uid, operation,
				command, state, created_at, updated_at
			) VALUES ($1, 'session', 'sandbox', '00000000-0000-0000-0000-000000000001', $2,
				$3, 'RUNNER_ADMITTED', now(), now())`, row.commandID, row.operation, []byte{})
		if err != nil {
			t.Fatalf("insert %s dispatch: %v", row.operation, err)
		}
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("upgrade predecessor schema:", err)
	}
	var count int
	if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM dispatch").Scan(&count); err != nil {
		t.Fatal("count upgraded dispatches:", err)
	}
	if count != 1 {
		t.Fatalf("remaining dispatch count = %d, want 1", count)
	}
	var commandID string
	if err := db.QueryRowContext(t.Context(), "SELECT command_id FROM dispatch").Scan(&commandID); err != nil {
		t.Fatal("read upgraded dispatch:", err)
	}
	if commandID != "send-1" {
		t.Fatalf("remaining dispatch command_id = %q, want send-1", commandID)
	}
}

func TestUpgradeFromVersionThreeClearsEmptyAdmissions(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 3); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
	for _, row := range []struct {
		commandID string
		state     string
		admission []byte
	}{
		{commandID: "claimed", state: "DELIVERING_TO_RUNNER", admission: []byte{}},
		{commandID: "admitted", state: "RUNNER_ADMITTED", admission: []byte{0x08, 0x01}},
	} {
		_, err := db.ExecContext(t.Context(), `
			INSERT INTO dispatch (
				command_id, session_id, sandbox_name, sandbox_uid, operation,
				command, state, admission, created_at, updated_at
			) VALUES ($1, 'session', 'sandbox', '00000000-0000-0000-0000-000000000001', 'submit_input',
				$2, $3, $4, $5, $5)`, row.commandID, []byte{}, row.state, row.admission, now)
		if err != nil {
			t.Fatalf("insert %s dispatch: %v", row.commandID, err)
		}
	}

	if err := Up(t.Context(), db); err != nil {
		t.Fatal("upgrade predecessor schema:", err)
	}
	rows, err := db.QueryContext(t.Context(), "SELECT command_id, admission IS NULL, admission FROM dispatch ORDER BY command_id")
	if err != nil {
		t.Fatal("read upgraded admissions:", err)
	}
	defer rows.Close()
	got := make(map[string]struct {
		isNull bool
		data   []byte
	})
	for rows.Next() {
		var commandID string
		var value struct {
			isNull bool
			data   []byte
		}
		if err := rows.Scan(&commandID, &value.isNull, &value.data); err != nil {
			t.Fatal("scan upgraded admission:", err)
		}
		got[commandID] = value
	}
	if err := rows.Err(); err != nil {
		t.Fatal("iterate upgraded admissions:", err)
	}
	if len(got) != 2 || !got["claimed"].isNull || got["admitted"].isNull || !bytes.Equal(got["admitted"].data, []byte{0x08, 0x01}) {
		t.Fatalf("upgraded admissions = %#v, want claimed NULL and admitted 0801", got)
	}
}

func TestWakeAssessmentDefaultsSupportPreviousWritersDuringUpgrade(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 7); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	if err := insertPreviousDispatchWriterRow(t.Context(), db, "dispatch-before-upgrade"); err != nil {
		t.Fatal("insert predecessor dispatch:", err)
	}
	if err := insertPreviousSessionWriterRow(t.Context(), db, "opening-before-upgrade"); err != nil {
		t.Fatal("insert predecessor opening:", err)
	}
	if _, err := provider.UpTo(t.Context(), 8); err != nil {
		t.Fatal("apply wake assessment migration:", err)
	}

	// These statements match the predecessor Gateway's INSERT column sets. Old
	// replicas omit both new columns while they overlap the upgraded schema.
	if err := insertPreviousDispatchWriterRow(t.Context(), db, "dispatch-during-upgrade"); err != nil {
		t.Fatal("previous dispatch writer after upgrade:", err)
	}
	if err := insertPreviousSessionWriterRow(t.Context(), db, "opening-during-upgrade"); err != nil {
		t.Fatal("previous opening writer after upgrade:", err)
	}
	if err := insertCurrentDispatchWriterRow(t.Context(), db, "dispatch-current-writer"); err != nil {
		t.Fatal("current dispatch writer after upgrade:", err)
	}
	if err := insertCurrentSessionWriterRow(t.Context(), db, "opening-current-writer"); err != nil {
		t.Fatal("current opening writer after upgrade:", err)
	}

	for _, row := range []struct {
		table string
		key   string
		id    string
	}{
		{table: "dispatch", key: "command_id", id: "dispatch-before-upgrade"},
		{table: "session_opening", key: "session_id", id: "opening-before-upgrade"},
		{table: "dispatch", key: "command_id", id: "dispatch-during-upgrade"},
		{table: "session_opening", key: "session_id", id: "opening-during-upgrade"},
	} {
		var wake, blocker []byte
		query := "SELECT wake_assessment, delivery_blocker FROM " + row.table + " WHERE " + row.key + "=$1"
		if err := db.QueryRowContext(t.Context(), query, row.id).Scan(&wake, &blocker); err != nil {
			t.Fatalf("read previous-writer row %s: %v", row.id, err)
		}
		if !bytes.Equal(wake, []byte{0x08, 0x01}) || blocker != nil {
			t.Fatalf("previous-writer row %s = wake %x, blocker %x; want NOT_EVALUATED and NULL", row.id, wake, blocker)
		}
	}
	for _, row := range []struct {
		table string
		key   string
		id    string
	}{
		{table: "dispatch", key: "command_id", id: "dispatch-current-writer"},
		{table: "session_opening", key: "session_id", id: "opening-current-writer"},
	} {
		var wake []byte
		query := "SELECT wake_assessment FROM " + row.table + " WHERE " + row.key + "=$1"
		if err := db.QueryRowContext(t.Context(), query, row.id).Scan(&wake); err != nil {
			t.Fatalf("read current-writer row %s: %v", row.id, err)
		}
		if !bytes.Equal(wake, []byte{0x08, 0x02}) {
			t.Fatalf("current-writer row %s wake = %x, want explicit NOT_NEEDED", row.id, wake)
		}
	}
}

func insertPreviousDispatchWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
		 operation,command,state,traceparent,actor_account_namespace,actor_account_name,actor_account_uid,actor_pod_name,actor_pod_uid,
		 effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,action_request_id,action_execution_id,
		 created_at,updated_at)
		VALUES ($1,'session','sandbox','00000000-0000-0000-0000-000000000001',false,NULL,NULL,
		 'submit_input',decode('','hex'),'QUEUED_IN_DISPATCHER','',NULL,NULL,NULL,NULL,NULL,
		 NULL,NULL,NULL,NULL,NULL,now(),now())`, id)
	return err
}

func insertPreviousSessionWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO session_opening
		(session_id,sandbox_name,sandbox_uid,execution_account_namespace,execution_account_name,execution_account_uid,
		 creator_account_namespace,creator_account_name,creator_account_uid,effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
		 creator_pod_name,creator_pod_uid,action_request_id,action_execution_id,wake_eligible,wake_caller_namespace,wake_caller_name,
		 spec,setup_script,state,traceparent,created_at,updated_at)
		VALUES ($1,'sandbox','00000000-0000-0000-0000-000000000001',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
		 NULL,NULL,NULL,NULL,false,NULL,NULL,decode('0801','hex'),NULL,'QUEUED_IN_DISPATCHER','',now(),now())`, id)
	return err
}

func insertCurrentDispatchWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at,delivery_blocker,wake_assessment)
		VALUES ($1,'session','sandbox','00000000-0000-0000-0000-000000000001','submit_input',decode('','hex'),
		 'QUEUED_IN_DISPATCHER',now(),now(),NULL,decode('0802','hex'))`, id)
	return err
}

func insertCurrentSessionWriterRow(ctx context.Context, db *sql.DB, id string) error {
	_, err := db.ExecContext(ctx, `INSERT INTO session_opening
		(session_id,sandbox_name,sandbox_uid,spec,state,created_at,updated_at,delivery_blocker,wake_assessment)
		VALUES ($1,'sandbox','00000000-0000-0000-0000-000000000001',decode('0801','hex'),
		 'QUEUED_IN_DISPATCHER',now(),now(),NULL,decode('0802','hex'))`, id)
	return err
}

func TestWakeAttemptCallerUIDExpandsUpgradeKey(t *testing.T) {
	db := openMigrationDatabase(t)
	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 7); err != nil {
		t.Fatal("apply predecessor schema:", err)
	}
	const sandboxUID = "00000000-0000-0000-0000-000000000001"
	if _, err := db.ExecContext(t.Context(), `INSERT INTO wake_attempt
		(caller_namespace,caller_name,sandbox_uid,wake_generation,attempted_at)
		VALUES ('test-namespace','test-account',$1,1,now())`, sandboxUID); err != nil {
		t.Fatal("insert predecessor wake attempt:", err)
	}
	if _, err := provider.UpTo(t.Context(), 8); err != nil {
		t.Fatal("apply wake attempt migration:", err)
	}

	var callerUID string
	if err := db.QueryRowContext(t.Context(), `SELECT caller_uid FROM wake_attempt
		WHERE caller_namespace='test-namespace' AND caller_name='test-account' AND sandbox_uid=$1 AND wake_generation=1`, sandboxUID).Scan(&callerUID); err != nil {
		t.Fatal("read upgraded wake attempt:", err)
	}
	if callerUID != "" {
		t.Fatalf("upgraded caller UID = %q, want empty for the predecessor writer", callerUID)
	}

	// The predecessor insert shape omits caller_uid. A current caller UID can
	// then record the same target generation without colliding with that row.
	if _, err := db.ExecContext(t.Context(), `INSERT INTO wake_attempt
		(caller_namespace,caller_name,sandbox_uid,wake_generation,attempted_at)
		VALUES ('test-namespace','test-account',$1,2,now())`, sandboxUID); err != nil {
		t.Fatal("insert predecessor-shaped wake attempt after upgrade:", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO wake_attempt
		(caller_namespace,caller_name,caller_uid,sandbox_uid,wake_generation,attempted_at)
		VALUES ('test-namespace','test-account','uid-2',$1,1,now())`, sandboxUID); err != nil {
		t.Fatal("insert UID-scoped wake attempt:", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO wake_attempt
		(caller_namespace,caller_name,caller_uid,sandbox_uid,wake_generation,attempted_at)
		VALUES ('test-namespace','test-account','uid-2',$1,1,now())`, sandboxUID); err == nil {
		t.Fatal("duplicate UID-scoped wake attempt succeeded; want primary-key conflict")
	}
}

func openMigrationDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal("open PostgreSQL clone:", err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Errorf("close PostgreSQL clone: %v", err)
		}
	})
	return db
}
