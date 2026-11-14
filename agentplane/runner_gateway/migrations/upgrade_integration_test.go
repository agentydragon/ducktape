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

func TestCutoverRemovesSessionsAndCommandsWithoutIdentityOrAttribution(t *testing.T) {
	db := openMigrationDatabase(t)
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("create final schema:", err)
	}
	const sandboxUID = "00000000-0000-0000-0000-000000000018"
	const executionIdentityUID = "00000000-0000-4000-8000-000000000018"
	insertSession := func(sessionID string, identityName, identityUID any, actorKind string) {
		t.Helper()
		var creatorAccountNamespace, creatorAccountName, creatorAccountUID any
		var effectiveAccountNamespace, effectiveAccountName, effectiveAccountUID any
		var creatorIdentityName, creatorIdentityUID any
		switch actorKind {
		case "service_account":
			creatorAccountNamespace, creatorAccountName, creatorAccountUID = "test", "actor", "actor-uid"
			effectiveAccountNamespace, effectiveAccountName, effectiveAccountUID = "test", "actor", "actor-uid"
		case "identity":
			creatorIdentityName, creatorIdentityUID = "creator", "creator-uid"
		case "none":
		default:
			t.Fatalf("unknown actor kind %q", actorKind)
		}
		_, err := db.ExecContext(t.Context(), `INSERT INTO session
			(session_id,sandbox_name,sandbox_uid,spec,state,created_at,updated_at,
			 creator_account_namespace,creator_account_name,creator_account_uid,
			 effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
			 creator_identity_name,creator_identity_uid,execution_identity_name,execution_identity_uid)
			VALUES ($1,'test-sandbox',$2,$3,'WAITING',now(),now(),$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
			sessionID, sandboxUID, []byte{0x08, 0x01}, creatorAccountNamespace, creatorAccountName, creatorAccountUID,
			effectiveAccountNamespace, effectiveAccountName, effectiveAccountUID, creatorIdentityName, creatorIdentityUID,
			identityName, identityUID)
		if err != nil {
			t.Fatalf("insert Session %q: %v", sessionID, err)
		}
	}
	insertDispatch := func(commandID, sessionID, actorKind string) {
		t.Helper()
		var actorNamespace, actorName, actorUID, actorIdentityName, actorIdentityUID any
		switch actorKind {
		case "service_account":
			actorNamespace, actorName, actorUID = "test", "actor", "actor-uid"
		case "identity":
			actorIdentityName, actorIdentityUID = "actor", "actor-uid"
		case "none":
		default:
			t.Fatalf("unknown actor kind %q", actorKind)
		}
		_, err := db.ExecContext(t.Context(), `INSERT INTO dispatch
			(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at,
			 actor_account_namespace,actor_account_name,actor_account_uid,actor_identity_name,actor_identity_uid)
			VALUES ($1,$2,'test-sandbox',$3,'submit_input',$4,'QUEUED',now(),now(),$5,$6,$7,$8,$9)`,
			commandID, sessionID, sandboxUID, []byte{0x08, 0x01}, actorNamespace, actorName, actorUID, actorIdentityName, actorIdentityUID)
		if err != nil {
			t.Fatalf("insert dispatch %q: %v", commandID, err)
		}
	}

	insertSession("pre-identity-session", nil, nil, "service_account")
	insertDispatch("pre-identity-command", "pre-identity-session", "service_account")
	insertSession("unattributed-session", "execution", executionIdentityUID, "none")
	insertDispatch("unattributed-session-command", "unattributed-session", "service_account")
	insertSession("current-session", "execution", executionIdentityUID, "service_account")
	insertDispatch("current-command", "current-session", "service_account")
	insertDispatch("unattributed-command", "current-session", "none")
	insertSession("identity-attributed-session", "execution", executionIdentityUID, "identity")
	insertDispatch("identity-attributed-command", "identity-attributed-session", "identity")

	if _, err := db.ExecContext(t.Context(), "DELETE FROM "+gooseTable+" WHERE version_id=18"); err != nil {
		t.Fatal("reset migration 18 version:", err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal("apply cutover migration:", err)
	}

	for _, query := range []string{
		`SELECT count(*) FROM session WHERE session_id IN ('pre-identity-session','unattributed-session')`,
		`SELECT count(*) FROM dispatch WHERE command_id IN ('pre-identity-command','unattributed-session-command','unattributed-command')`,
	} {
		var count int
		if err := db.QueryRowContext(t.Context(), query).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 0 {
			t.Fatalf("cutover retained %d rows for %s", count, query)
		}
	}
	for _, query := range []string{
		`SELECT count(*) FROM session WHERE session_id IN ('current-session','identity-attributed-session')`,
		`SELECT count(*) FROM dispatch WHERE command_id IN ('current-command','identity-attributed-command')`,
	} {
		var count int
		if err := db.QueryRowContext(t.Context(), query).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 2 {
			t.Fatalf("cutover lost attributed evidence: count=%d for %s", count, query)
		}
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
	    (session_id,sandbox_name,sandbox_uid,spec,state,reason,created_at,updated_at,
	     creator_account_namespace,creator_account_name,creator_account_uid,
	     effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
	     execution_identity_name,execution_identity_uid,environment_deleted_at)
	    VALUES ('retained-session','deleted-environment',$1,$2,'UNKNOWN','effect_unconfirmed',now(),now(),
	     'test','owner','old-service-account-uid','test','owner','old-service-account-uid',
	     'retained-identity','00000000-0000-4000-8000-000000000001',now())`, uid, []byte{0x08, 0x01})
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.ExecContext(ctx, `INSERT INTO dispatch
	    (command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,reason,admission,delivery_attempted,created_at,updated_at,
	     actor_account_namespace,actor_account_name,actor_account_uid,
	     effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid)
	    VALUES ('uncertain-command','retained-session','deleted-environment',$1,'submit_input',$2,'UNKNOWN','effect_unconfirmed',$3,true,now(),now(),
	     'test','owner','old-service-account-uid','test','owner','old-service-account-uid')`,
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
	var ownerUID, identityName, reason string
	var deletedAt sql.NullTime
	if err := db.QueryRowContext(t.Context(), `SELECT spec,creator_account_uid,execution_identity_name,environment_deleted_at FROM session WHERE session_id='retained-session'`).Scan(&spec, &ownerUID, &identityName, &deletedAt); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(spec, []byte{0x08, 0x01}) || ownerUID != "old-service-account-uid" || identityName != "retained-identity" || !deletedAt.Valid {
		t.Fatalf("Session evidence changed: spec=%x owner=%q identity=%q deletion=%v", spec, ownerUID, identityName, deletedAt)
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
