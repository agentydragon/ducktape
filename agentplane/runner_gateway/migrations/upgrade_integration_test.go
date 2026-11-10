package migrations

import (
	"bytes"
	"database/sql"
	"strings"
	"testing"

	_ "github.com/lib/pq"
)

const gooseTable = "runner_gateway_dispatcher_goose_version"

func TestBaselineCreatesFinalSchema(t *testing.T) {
	db := openMigrationDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	insertRetainedEvidence(t, db)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("repeat baseline:", err)
	}
	assertRetainedEvidence(t, db)
}

func TestBaselineRejectsUnknownObjectsInFreshDatabase(t *testing.T) {
	db := openMigrationDatabase(t)
	if _, err := db.ExecContext(t.Context(), `CREATE TABLE unrecognized_data (value text); INSERT INTO unrecognized_data VALUES ('keep')`); err != nil {
		t.Fatal(err)
	}
	if err := Up(t.Context(), db); err == nil {
		t.Fatal("baseline accepted an unrecognized database")
	}
	var value string
	if err := db.QueryRowContext(t.Context(), `SELECT value FROM unrecognized_data`).Scan(&value); err != nil || value != "keep" {
		t.Fatalf("unrecognized data changed: %q, %v", value, err)
	}
}

func TestBaselineRejectsSchemaWithoutHistory(t *testing.T) {
	db := openMigrationDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), "DELETE FROM "+gooseTable+" WHERE version_id>=14"); err != nil {
		t.Fatal(err)
	}
	err := Up(t.Context(), db)
	if err == nil || !strings.Contains(err.Error(), "previous image") {
		t.Fatalf("history-less schema error = %v", err)
	}
}

func insertRetainedEvidence(t *testing.T, db *sql.DB) {
	t.Helper()
	ctx := t.Context()
	const uid = "00000000-0000-0000-0000-000000000001"
	_, err := db.ExecContext(ctx, `INSERT INTO session
        (session_id,sandbox_name,sandbox_uid,spec,state,reason,created_at,updated_at,creator_account_namespace,creator_account_name,creator_account_uid,environment_deleted_at)
        VALUES ('retained-session','deleted-environment',$1,$2,'UNKNOWN','effect_unconfirmed',now(),now(),'test','owner','old-service-account-uid',now())`, uid, []byte{0x08, 0x01})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.ExecContext(ctx, `INSERT INTO dispatch
        (command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,reason,admission,delivery_attempted,created_at,updated_at)
        VALUES ('uncertain-command','retained-session','deleted-environment',$1,'submit_input',$2,'UNKNOWN','effect_unconfirmed',$3,true,now(),now())`,
		uid, []byte{0x08, 0x01}, []byte{0x08, 0x02})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.ExecContext(ctx, `INSERT INTO deleted_sandbox (sandbox_uid) VALUES ($1)`, uid)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.ExecContext(ctx, `INSERT INTO wake_attempt
        (caller_namespace,caller_name,caller_uid,sandbox_uid,wake_generation,attempted_at)
        VALUES ('test','caller','caller-uid',$1,1,now())`, uid)
	if err != nil {
		t.Fatal(err)
	}
}

func assertRetainedEvidence(t *testing.T, db *sql.DB) {
	t.Helper()
	var spec, command, admission []byte
	var ownerUID, reason string
	var deletedAt sql.NullTime
	if err := db.QueryRowContext(t.Context(), `SELECT spec,creator_account_uid,environment_deleted_at FROM session WHERE session_id='retained-session'`).Scan(&spec, &ownerUID, &deletedAt); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(spec, []byte{0x08, 0x01}) || ownerUID != "old-service-account-uid" || !deletedAt.Valid {
		t.Fatalf("Session evidence changed: spec=%x owner=%q deletion=%v", spec, ownerUID, deletedAt)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT command,admission,reason FROM dispatch WHERE command_id='uncertain-command'`).Scan(&command, &admission, &reason); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(command, []byte{0x08, 0x01}) || !bytes.Equal(admission, []byte{0x08, 0x02}) || reason != "effect_unconfirmed" {
		t.Fatalf("receipt evidence changed: command=%x admission=%x reason=%q", command, admission, reason)
	}
	for _, query := range []string{
		`SELECT count(*) FROM deleted_sandbox WHERE sandbox_uid='00000000-0000-0000-0000-000000000001'`,
		`SELECT count(*) FROM wake_attempt WHERE caller_uid='caller-uid'`,
	} {
		var count int
		if err := db.QueryRowContext(t.Context(), query).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 1 {
			t.Fatalf("retained UID fence count=%d for %s", count, query)
		}
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
