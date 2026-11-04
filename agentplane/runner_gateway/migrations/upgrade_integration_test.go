package migrations

import (
	"bytes"
	"database/sql"
	"strings"
	"sync"
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

func TestBaselineAdoptsCompletedVersion13WithoutRewritingEvidence(t *testing.T) {
	db := openMigrationDatabase(t)
	prepareVersion13(t, db)
	insertRetainedEvidence(t, db)

	var wait sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			errors <- Up(t.Context(), db)
		}()
	}
	wait.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal("concurrent adoption:", err)
		}
	}
	assertRetainedEvidence(t, db)
	var oldCount, newCount int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FILTER (WHERE version_id BETWEEN 2 AND 13),
        count(*) FILTER (WHERE version_id=14 AND is_applied) FROM `+gooseTable).Scan(&oldCount, &newCount); err != nil {
		t.Fatal(err)
	}
	if oldCount != 12 || newCount != 1 {
		t.Fatalf("Goose history: old=%d new=%d, want 12 and 1", oldCount, newCount)
	}
}

func TestBaselineRejectsPartialPredecessors(t *testing.T) {
	for _, test := range []struct {
		name   string
		damage func(*testing.T, *sql.DB)
	}{
		{"missing_version", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), "DELETE FROM "+gooseTable+" WHERE version_id=8")
			if err != nil {
				t.Fatal(err)
			}
		}},
		{"unapplied_version", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), "UPDATE "+gooseTable+" SET is_applied=false WHERE version_id=8")
			if err != nil {
				t.Fatal(err)
			}
		}},
		{"missing_uid_evidence", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), "ALTER TABLE session DROP COLUMN creator_account_uid")
			if err != nil {
				t.Fatal(err)
			}
		}},
		{"missing_receipt_index", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), "DROP INDEX ix_dispatch_session_state")
			if err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			db := openMigrationDatabase(t)
			prepareVersion13(t, db)
			insertRetainedEvidence(t, db)
			test.damage(t, db)
			err := Up(t.Context(), db)
			if err == nil || !strings.Contains(err.Error(), "previous image") {
				t.Fatalf("adoption error = %v, want instruction to run previous image", err)
			}
			var adopted int
			if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM "+gooseTable+" WHERE version_id=14 AND is_applied").Scan(&adopted); err != nil {
				t.Fatal(err)
			}
			if adopted != 0 {
				t.Fatal("incomplete predecessor was adopted")
			}
			var receipts int
			if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM dispatch WHERE command_id='uncertain-command'").Scan(&receipts); err != nil {
				t.Fatal(err)
			}
			if receipts != 1 {
				t.Fatal("rejected adoption lost command receipt")
			}
		})
	}
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
	if _, err := db.ExecContext(t.Context(), "DELETE FROM "+gooseTable+" WHERE version_id=14"); err != nil {
		t.Fatal(err)
	}
	err := Up(t.Context(), db)
	if err == nil || !strings.Contains(err.Error(), "previous image") {
		t.Fatalf("history-less schema error = %v", err)
	}
}

func prepareVersion13(t *testing.T, db *sql.DB) {
	t.Helper()
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("create final schema:", err)
	}
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(t.Context(), "DELETE FROM "+gooseTable+" WHERE version_id=14"); err != nil {
		t.Fatal(err)
	}
	for version := 2; version <= 13; version++ {
		if _, err := tx.ExecContext(t.Context(), "INSERT INTO "+gooseTable+" (version_id,is_applied,tstamp) VALUES ($1,true,now())", version); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
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
