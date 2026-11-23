package migrations

import (
	"testing"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
)

// A legacy writer could retain any spelling. Only the literal UNKNOWN belonged
// to the recent-issues index; every unrecognized delivery state projected as UNKNOWN.
func TestNumericConsumerReadsHistoricalStates(t *testing.T) {
	ctx := t.Context()
	db := openMigrationDatabase(t)
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(ctx, 28); err != nil {
		t.Fatal(err)
	}
	insertRetainedEvidence(t, db)
	if _, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at)
		SELECT 'historical-command',session_id,sandbox_name,sandbox_uid,operation,command,'older-state',created_at,updated_at
		FROM dispatch WHERE command_id='uncertain-command'`); err != nil {
		t.Fatal(err)
	}
	// Pre-retention Dispatchers physically deleted these rows when an environment went away.
	if _, err := db.ExecContext(ctx, `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at)
		SELECT 'deleted-' || command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,created_at,updated_at
		FROM dispatch WHERE session_id='retained-session';
		DELETE FROM dispatch WHERE command_id IN ('deleted-uncertain-command','deleted-historical-command')`); err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(ctx, 31); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, "CALL backfill_dispatch_enum_values(1)"); err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(ctx, 33); err != nil {
		t.Fatal(err)
	}
	consumer := store.NewStore(db, 4)
	caller := store.ServiceCaller{}
	const sessionID = "retained-session"
	page, _, err := consumer.ReadDispatchPage(ctx, sessionID, caller, 10, store.DispatchPageCursor{})
	if err != nil || len(page.GetDispatches()) != 2 {
		t.Fatalf("page = %v, error %v", page, err)
	}
	for _, receipt := range page.GetDispatches() {
		if receipt.State != store.UnknownState {
			t.Errorf("receipt %q has state %v", receipt.CommandId, receipt.State)
		}
	}
	workset, _, err := consumer.ReadDispatchWorkset(ctx, sessionID, caller, 0, false)
	if err != nil || len(workset.GetRecentIssues()) != 2 {
		t.Fatalf("recent issues = %v, error %v", workset, err)
	}
	want := map[string]bool{"uncertain-command": false, "deleted-uncertain-command": true}
	for _, issue := range workset.GetRecentIssues() {
		wasDeleted, ok := want[issue.CommandId]
		if !ok || issue.LegacyDeletionReceipt != wasDeleted {
			t.Fatalf("unexpected recent issue %v", issue)
		}
		delete(want, issue.CommandId)
	}
	filter := &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: sessionID}}
	changes, err := consumer.ReadDispatchChanges(ctx, filter, caller, 0, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range changes.Changes {
		if change.Dispatch.GetCommandId() == "deleted-historical-command" && change.Deleted && change.Dispatch.GetState() == store.UnknownState {
			return
		}
	}
	t.Fatalf("missing historical deletion receipt in %v", changes.Changes)
}
