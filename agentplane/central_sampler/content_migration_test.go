package centralsampler

import (
	"testing"
)

func newInlineTestStore(t *testing.T) *Store {
	t.Helper()
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	provider, err := schema.Provider(store.DB())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 12); err != nil {
		t.Fatal(err)
	}
	return store
}

func TestNativeContentMigrationResetsStagingStateAtomically(t *testing.T) {
	store := newInlineTestStore(t)
	migrationExec(t, store.DB(), `
		INSERT INTO sampler_conversation(id,api,context)
			VALUES('00000000-0000-0000-0000-000000000001','responses','{"old_context":true}');
		INSERT INTO sampler_conversation_item(conversation_id,ordinal,record)
			VALUES('00000000-0000-0000-0000-000000000001',1,'{"old_record":true}');
		INSERT INTO sampler_intent(id,conversation_id,generation,request)
			VALUES('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001',0,'{"body":{"old_request":true}}');
		INSERT INTO sampler_process(id,conversation_id,generation,status,tty)
			VALUES('process','00000000-0000-0000-0000-000000000001',0,'unknown',false);
		INSERT INTO sampler_cell(id,conversation_id,generation,status)
			VALUES('cell','00000000-0000-0000-0000-000000000001',0,'live');
		INSERT INTO sampler_interrupt(id,conversation_id,generation)
			VALUES('00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000001',0);
		INSERT INTO sampler_session(id,conversation_id,account_uid,configuration,model,effort,source_id)
			VALUES('session','00000000-0000-0000-0000-000000000001','account','','model','high','source');
		INSERT INTO sampler_command(session_id,id,payload,admission,outcome) VALUES('session','command','',1,1);
		INSERT INTO sampler_event(session_id,sequence,entry) VALUES('session',1,'');
		INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES('session','history',1);
		INSERT INTO sampler_notification(conversation_id,call_id,cell_id,text)
			VALUES('00000000-0000-0000-0000-000000000001','call','cell','unread');
	`)
	// Force a DDL failure after the reset statements. Goose must roll back the
	// entire migration, including TRUNCATE and retirement of the inline schema.
	migrationExec(t, store.DB(), `CREATE TABLE sampler_content (collision TEXT)`)
	if err := Migrate(t.Context(), store.DB()); err == nil {
		t.Fatal("migration succeeded despite a conflicting table")
	}
	tables := []string{
		"sampler_conversation", "sampler_intent", "sampler_process", "sampler_cell", "sampler_interrupt",
		"sampler_session", "sampler_command", "sampler_event", "sampler_export_checkpoint", "sampler_notification",
	}
	for _, table := range append(tables, "sampler_conversation_item") {
		if count := migrationCount(t, store.DB(), "SELECT count(*) FROM "+table); count != 1 {
			t.Fatalf("failed migration changed %s: rows=%d", table, count)
		}
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=13 AND is_applied`); count != 0 {
		t.Fatal("failed migration recorded success")
	}
	migrationExec(t, store.DB(), `DROP TABLE sampler_content`)
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	for _, table := range append(tables, "sampler_content", "sampler_context", "sampler_model_request") {
		if count := migrationCount(t, store.DB(), "SELECT count(*) FROM "+table); count != 0 {
			t.Fatalf("migration retained %s: rows=%d", table, count)
		}
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM information_schema.tables
		WHERE table_schema=current_schema() AND table_name='sampler_conversation_item'`); count != 0 {
		t.Fatal("inline transcript table remains")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name='sampler_conversation' AND column_name='context'`); count != 0 {
		t.Fatal("inline active-context column remains")
	}
	// Once version 13 is applied, ordinary restarts must retain newly created state.
	session := openSessionForStore(t, store, "")
	if err := Migrate(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetSession(t.Context(), session.ID, ""); err != nil {
		t.Fatalf("restart lost a new-format session: %v", err)
	}
}
