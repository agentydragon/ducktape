package sessionstore

import (
	"errors"
	"fmt"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"google.golang.org/protobuf/proto"
)

func TestCommandAdmissionRowServesCurrentResponseWithoutJournal(t *testing.T) {
	store, session, _ := claimedSession(t)
	command := &commandpb.Command{
		CommandId: "indexed-admission",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "keep the original response"}},
	}
	wantAdmission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil {
		t.Fatal(err)
	}

	const failureReason = "provider execution failed"
	result, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_command SET outcome=$3,failure_reason=$4
		WHERE session_id=$1 AND id=$2`, session.ID, command.GetCommandId(),
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED), failureReason)
	if err != nil {
		t.Fatal(err)
	}
	if count, err := result.RowsAffected(); err != nil || count != 1 {
		t.Fatalf("updated command rows=%d err=%v", count, err)
	}

	// Make any accidental journal lookup fail. The command row still has the stable response
	// metadata and the latest outcome/failure state.
	migrationExec(t, store.DB(), `ALTER TABLE sampler_event RENAME TO sampler_event_unavailable`)
	migrationExec(t, store.DB(), `ALTER TABLE sampler_event_block RENAME TO sampler_event_block_unavailable`)

	got, err := store.GetCommand(t.Context(), session.ID, command.GetCommandId())
	if err != nil || !proto.Equal(got.GetAdmission(), wantAdmission) {
		t.Fatalf("indexed GetCommand admission=%v err=%v", got, err)
	}
	if got.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED || got.GetFailureReason() != failureReason {
		t.Fatalf("GetCommand outcome=%v failure=%q", got.GetOutcome(), got.GetFailureReason())
	}

	gotAdmission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil || !proto.Equal(gotAdmission, wantAdmission) {
		t.Fatalf("indexed admission replay=%v err=%v", gotAdmission, err)
	}
	changed := &commandpb.Command{
		CommandId: command.GetCommandId(),
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "different payload"}},
	}
	if _, err := store.Admit(t.Context(), session.ID, changed, session.OwnerKey, nil); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed duplicate payload error=%v, want ErrConflict", err)
	}
	if _, err := store.Admit(t.Context(), session.ID, command, "other-owner", nil); !errors.Is(err, ErrForbidden) {
		t.Fatalf("other owner duplicate error=%v, want ErrForbidden", err)
	}
}

func TestCommandAdmissionMetadataMigrationPreservesPackedLegacyRows(t *testing.T) {
	store, session, owner := claimedSession(t)
	command := &commandpb.Command{
		CommandId: "packed-legacy-admission",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "packed legacy input"}},
	}
	wantAdmission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := range 20 {
		if err := store.ProtocolEvent(t.Context(), owner, fmt.Sprintf("migration-fixture-%d", i),
			eventpb.ExecutionProgress_KIND_EXECUTOR, eventpb.ExecutionProgress_PHASE_RECEIVED, map[string]any{"index": i}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.CompactJournal(t.Context(), session.ID, true); err != nil {
		t.Fatal(err)
	}
	var packed bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT EXISTS(
		SELECT 1 FROM sampler_event_block WHERE session_id=$1 AND first_sequence<=$2 AND last_sequence>=$2
	)`, session.ID, wantAdmission.GetCursor()).Scan(&packed); err != nil {
		t.Fatal(err)
	}
	if !packed {
		t.Fatalf("admission cursor %d was not packed before migration", wantAdmission.GetCursor())
	}

	// Recreate the pre-0024 schema while keeping the already-packed journal and command row. Reapply
	// 0024 to verify the migration leaves legacy rows NULL for the journal fallback.
	migrationExec(t, store.DB(), `ALTER TABLE sampler_command
		DROP CONSTRAINT sampler_command_admission_metadata_check,
		DROP COLUMN admission_origin_source_id,
		DROP COLUMN admission_event_at_seconds,
		DROP COLUMN admission_event_at_nanos`)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=24 AND is_applied`)
	if err := migrations.Up(t.Context(), store.DB()); err != nil {
		t.Fatal(err)
	}
	var metadataIsNull bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT admission_origin_source_id IS NULL
		AND admission_event_at_seconds IS NULL AND admission_event_at_nanos IS NULL
		FROM sampler_command WHERE session_id=$1 AND id=$2`, session.ID, command.GetCommandId()).Scan(&metadataIsNull); err != nil {
		t.Fatal(err)
	}
	if !metadataIsNull {
		t.Fatal("migration backfilled response metadata for the legacy row")
	}

	got, err := store.GetCommand(t.Context(), session.ID, command.GetCommandId())
	if err != nil || !proto.Equal(got.GetAdmission(), wantAdmission) {
		t.Fatalf("packed legacy GetCommand admission=%v err=%v", got, err)
	}
	if got.GetOutcome() != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED || got.GetFailureReason() != "" {
		t.Fatalf("packed legacy outcome=%v failure=%q", got.GetOutcome(), got.GetFailureReason())
	}
	gotAdmission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil || !proto.Equal(gotAdmission, wantAdmission) {
		t.Fatalf("packed legacy admission replay=%v err=%v", gotAdmission, err)
	}
}
