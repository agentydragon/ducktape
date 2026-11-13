package migrations

import (
	"database/sql"
	"embed"
	"io/fs"
	"strings"
	"testing"

	"github.com/pressly/goose/v3"
)

// These are the migration files shipped immediately before version 15, kept as test fixtures so
// the prior-v11 fresh-schema upgrade remains an executable compatibility check.
//
//go:embed testdata/legacy_v14/*.sql
var legacyV14Migrations embed.FS

func baselineDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func upBaseline(t *testing.T, db *sql.DB) error {
	t.Helper()
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	_, err = provider.UpTo(t.Context(), 15)
	return err
}

func previousV14Provider(t *testing.T, db *sql.DB) *goose.Provider {
	t.Helper()
	files, err := fs.Sub(legacyV14Migrations, "testdata/legacy_v14")
	if err != nil {
		t.Fatal(err)
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("history_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal(err)
	}
	return provider
}

func upPriorV14(t *testing.T, db *sql.DB) error {
	t.Helper()
	_, err := previousV14Provider(t, db).Up(t.Context())
	return err
}

func assertV14GooseCanReadV15Ledger(t *testing.T, db *sql.DB) {
	t.Helper()
	provider := previousV14Provider(t, db)
	pending, err := provider.HasPending(t.Context())
	if err != nil {
		t.Fatalf("v14 Goose provider reading v15 ledger: %v", err)
	}
	if pending {
		t.Fatal("v14 Goose provider found pending migrations on v15 ledger")
	}
}

func versionCount(t *testing.T, db *sql.DB, version int) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM history_service_goose_version WHERE version_id=$1 AND is_applied`, version).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func setHistoryVersion(t *testing.T, db *sql.DB, version int) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `DELETE FROM history_service_goose_version`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO history_service_goose_version(version_id,is_applied)
		SELECT generated.version_id,true FROM generate_series(1,$1) AS generated(version_id)`, version); err != nil {
		t.Fatal(err)
	}
}

func schemaObjectIdentities(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT object_identity FROM (
		SELECT concat('class:', relkind, ':', relname, ':', oid) AS object_identity
		FROM pg_class WHERE relnamespace=current_schema()::regnamespace
		UNION ALL
		SELECT concat('function:', proname, ':', oid, ':', pg_get_functiondef(oid))
		FROM pg_proc WHERE pronamespace=current_schema()::regnamespace
		UNION ALL
		SELECT concat('trigger:', tgname, ':', oid, ':', pg_get_triggerdef(oid))
		FROM pg_trigger WHERE tgrelid IN (
			SELECT oid FROM pg_class WHERE relnamespace=current_schema()::regnamespace)
		UNION ALL
		SELECT concat('constraint:', conname, ':', oid, ':', pg_get_constraintdef(oid))
		FROM pg_constraint WHERE conrelid IN (
			SELECT oid FROM pg_class WHERE relnamespace=current_schema()::regnamespace)
		UNION ALL
		SELECT concat('type:', typname, ':', oid) FROM pg_type
		WHERE typnamespace=current_schema()::regnamespace
		UNION ALL
		SELECT concat('publication:', pubname, ':', oid) FROM pg_publication
		WHERE pubname='electric_publication_agentplane_history'
	) AS schema_objects ORDER BY object_identity`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var result []string
	for rows.Next() {
		var object string
		if err := rows.Scan(&object); err != nil {
			t.Fatal(err)
		}
		result = append(result, object)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

func commandBridgeCount(t *testing.T, db *sql.DB) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT
		(SELECT count(*) FROM pg_trigger WHERE tgrelid='thread_entity'::regclass AND NOT tgisinternal
			AND tgname='thread_entity_command_operation_enum') +
		(SELECT count(*) FROM pg_proc WHERE pronamespace=current_schema()::regnamespace
			AND proname='thread_command_operation_enum')`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestBaselineCreatesVersion15HistorySchema(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	if got := versionCount(t, db, 15); got != 1 {
		t.Fatalf("baseline version records = %d", got)
	}
	if got := commandBridgeCount(t, db); got != 0 {
		t.Fatalf("temporary command bridge objects = %d", got)
	}
	const id = "00000000-0000-4000-8000-000000000015"
	var position int64
	if err := db.QueryRowContext(t.Context(), `INSERT INTO session_history(id,last_cursor,writer_identity_name,writer_identity_uid)
		VALUES($1,0,'identity','identity-uid') RETURNING recorded_position`, id).Scan(&position); err != nil {
		t.Fatal(err)
	}
	if position < 1 {
		t.Fatalf("recorded position = %d", position)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,1,$2,3)`, id, []byte("retained")); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,state,entity_index)
		VALUES($1,'epoch','item','one',1,1,false,'{}',1)`, id); err != nil {
		t.Fatal(err)
	}
	var pending int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM session_fold_pending WHERE session_id=$1`, id).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending != 1 {
		t.Fatalf("new session pending folds = %d", pending)
	}
	var published int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_publication_tables WHERE pubname='electric_publication_agentplane_history'`).Scan(&published); err != nil {
		t.Fatal(err)
	}
	if published != 4 {
		t.Fatalf("Electric published tables = %d", published)
	}
}

func TestBaselineAdoptsVersion14AndPreservesHistory(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	setHistoryVersion(t, db, 14)

	const id = "00000000-0000-4000-8000-000000000014"
	const eventPayload = "version-14 event"
	const entityState = `{"operation":"OPERATION_COMPACT","retained":true}`
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(
		id,last_cursor,feed_state,writer_identity_name,writer_identity_uid,
		execution_principal_namespace,execution_principal_name,execution_principal_uid,
		execution_identity_name,execution_identity_uid)
		VALUES($1,7,$2,'identity','identity-uid','namespace','runner','runner-uid','identity','identity-uid')`,
		id, []byte("retained feed state")); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,7,$2,3)`, id, []byte(eventPayload)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_entity(thread_id,projection_epoch,entity_kind,entity_id,cursor,revision_cursor,pending,state,entity_index)
		VALUES($1,'history-v1','command','compact',7,7,false,$2,1)`, id, entityState); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO thread_evidence(thread_id,projection_epoch,entity_cursor,observation_cursor)
		VALUES($1,'history-v1',7,7)`, id); err != nil {
		t.Fatal(err)
	}
	objectsBefore := schemaObjectIdentities(t, db)

	if err := upBaseline(t, db); err != nil {
		t.Fatal("adopt version-14 database:", err)
	}
	if objectsAfter := schemaObjectIdentities(t, db); strings.Join(objectsAfter, "\n") != strings.Join(objectsBefore, "\n") {
		t.Fatal("version-14 adoption changed persistent schema object identities")
	}
	for version := 1; version <= 15; version++ {
		if got := versionCount(t, db, version); got != 1 {
			t.Errorf("applied Goose version %d records = %d, want 1", version, got)
		}
	}
	assertV14GooseCanReadV15Ledger(t, db)
	if got := commandBridgeCount(t, db); got != 0 {
		t.Errorf("temporary command bridge objects = %d", got)
	}

	var gotFeed, gotEvent []byte
	var gotIdentityName, gotIdentityUID, gotPrincipalUID, gotExecutionName, gotExecutionUID string
	if err := db.QueryRowContext(t.Context(), `SELECT feed_state,writer_identity_name,writer_identity_uid,
		execution_principal_uid,execution_identity_name,execution_identity_uid FROM session_history WHERE id=$1`, id).
		Scan(&gotFeed, &gotIdentityName, &gotIdentityUID, &gotPrincipalUID, &gotExecutionName, &gotExecutionUID); err != nil {
		t.Fatal(err)
	}
	if string(gotFeed) != "retained feed state" || gotIdentityName != "identity" || gotIdentityUID != "identity-uid" ||
		gotPrincipalUID != "runner-uid" || gotExecutionName != "identity" || gotExecutionUID != "identity-uid" {
		t.Fatalf("retained session = feed %q, writer %q/%q, principal %q, execution %q/%q",
			gotFeed, gotIdentityName, gotIdentityUID, gotPrincipalUID, gotExecutionName, gotExecutionUID)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT payload FROM session_event WHERE session_id=$1 AND cursor=7`, id).Scan(&gotEvent); err != nil {
		t.Fatal(err)
	}
	if string(gotEvent) != eventPayload {
		t.Fatalf("retained event = %q, want %q", gotEvent, eventPayload)
	}
	var entityStateMatches bool
	if err := db.QueryRowContext(t.Context(), `SELECT state=$2::jsonb FROM thread_entity WHERE thread_id=$1 AND entity_id='compact'`, id, entityState).Scan(&entityStateMatches); err != nil {
		t.Fatal(err)
	}
	if !entityStateMatches {
		t.Fatalf("retained entity state does not match %s", entityState)
	}
	var evidenceCount int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM thread_evidence WHERE thread_id=$1 AND entity_cursor=7 AND observation_cursor=7`, id).Scan(&evidenceCount); err != nil {
		t.Fatal(err)
	}
	if evidenceCount != 1 {
		t.Fatalf("retained evidence rows = %d, want 1", evidenceCount)
	}
}

func TestBaselineAdoptsPriorV11FreshSchemaHistory(t *testing.T) {
	db := baselineDB(t)
	if err := upPriorV14(t, db); err != nil {
		t.Fatal("create fresh schema using the prior v11 baseline through v14:", err)
	}
	objectsBefore := schemaObjectIdentities(t, db)
	if err := upBaseline(t, db); err != nil {
		t.Fatal("adopt v14 schema created from the prior v11 fresh baseline:", err)
	}
	if objectsAfter := schemaObjectIdentities(t, db); strings.Join(objectsAfter, "\n") != strings.Join(objectsBefore, "\n") {
		t.Fatal("fresh-schema adoption changed persistent schema object identities")
	}
	for _, version := range []int{0, 11, 12, 13, 14, 15} {
		if got := versionCount(t, db, version); got != 1 {
			t.Errorf("applied Goose version %d records = %d, want 1", version, got)
		}
	}
	for _, version := range []int{1, 10} {
		if got := versionCount(t, db, version); got != 0 {
			t.Errorf("unexpected legacy Goose version %d records = %d", version, got)
		}
	}
	assertV14GooseCanReadV15Ledger(t, db)
}

func TestBaselineRejectsOlderDeployedVersionBeforeChangingData(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	setHistoryVersion(t, db, 13)
	const id = "00000000-0000-4000-8000-000000000013"
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor) VALUES($1,2)`, id); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,2,$2,3)`, id, []byte("still retained")); err != nil {
		t.Fatal(err)
	}

	err := upBaseline(t, db)
	if err == nil || !strings.Contains(err.Error(), "run the previous image through version 14 and let its rollout finish") {
		t.Fatalf("baseline error = %v, want an explicit version-14 rollout requirement", err)
	}
	if got := versionCount(t, db, 13); got != 1 {
		t.Errorf("older Goose history records = %d, want 1", got)
	}
	if got := versionCount(t, db, 15); got != 0 {
		t.Errorf("baseline Goose history records after rejection = %d, want 0", got)
	}
	var eventCount int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM session_event WHERE session_id=$1 AND payload=$2`, id, []byte("still retained")).Scan(&eventCount); err != nil {
		t.Fatal(err)
	}
	if eventCount != 1 {
		t.Fatalf("retained event rows after rejection = %d, want 1", eventCount)
	}
}

func TestBaselineRejectsIncompleteVersion14Schema(t *testing.T) {
	db := baselineDB(t)
	if err := upBaseline(t, db); err != nil {
		t.Fatal(err)
	}
	setHistoryVersion(t, db, 14)
	if _, err := db.ExecContext(t.Context(), `DROP INDEX ix_history_execution_identity_recorded`); err != nil {
		t.Fatal(err)
	}
	if err := upBaseline(t, db); err == nil || !strings.Contains(err.Error(), "version 14 schema is incomplete") {
		t.Fatalf("incomplete schema baseline error = %v", err)
	}
	if got := versionCount(t, db, 15); got != 0 {
		t.Fatalf("baseline Goose history records after rejection = %d, want 0", got)
	}
}
