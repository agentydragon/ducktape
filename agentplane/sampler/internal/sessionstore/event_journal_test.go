package sessionstore

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"google.golang.org/protobuf/proto"
)

func TestJournalBlockKeepsFragmentedEvidenceWithinDecodedLimit(t *testing.T) {
	store, session, owner := claimedSession(t)
	store.eventBlockWrites = true
	large := strings.Repeat("a", 2*BoundaryBytes+40)
	operation := "fragmented-evidence"
	if err := store.ProtocolEvent(t.Context(), owner, operation, eventpb.ExecutionProgress_KIND_EXECUTOR,
		eventpb.ExecutionProgress_PHASE_RECEIVED, map[string]any{"payload": large}); err != nil {
		t.Fatal(err)
	}
	for range eventcodec.MaxBlockEntries / 2 {
		if err := store.ProtocolEvent(t.Context(), owner, "other-evidence", eventpb.ExecutionProgress_KIND_EXECUTOR,
			eventpb.ExecutionProgress_PHASE_RECEIVED, map[string]any{"payload": "small evidence"}); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := store.Events(t.Context(), session.ID, session.Cursor, eventcodec.MaxBlockEntries)
	if err != nil {
		t.Fatal(err)
	}
	var data []byte
	var sequences []uint64
	foundProgress := false
	for _, entry := range entries {
		event := entry.GetEvent()
		if boundary := event.GetBoundaryRecord(); boundary != nil && boundary.GetCorrelationId() == operation {
			if boundary.GetFragmentCount() != 3 || boundary.GetFragmentIndex() != uint32(len(sequences)) {
				t.Fatalf("unexpected fragment %d of %d", boundary.GetFragmentIndex(), boundary.GetFragmentCount())
			}
			data = append(data, boundary.GetPayload()...)
			sequences = append(sequences, entry.GetOrigin().GetSequence())
		}
		if event.GetExecutionProgress().GetOperationId() == operation {
			foundProgress = true
			if !slices.Equal(sequences, event.GetSourceSequences()) {
				t.Fatalf("fragment sources=%v want=%v", event.GetSourceSequences(), sequences)
			}
		}
	}
	if !foundProgress {
		t.Fatal("missing progress for fragmented evidence")
	}
	var want map[string]string
	if err := json.Unmarshal(data, &want); err != nil || want["payload"] != large {
		t.Fatalf("fragment reconstruction failed (payload bytes=%d): %v", len(want["payload"]), err)
	}
}

func TestJournalBlockPreservesLegacyFramesAndCommandAdmission(t *testing.T) {
	store, session, owner := claimedSession(t)
	const window = eventcodec.MaxBlockEntries
	for cursor := session.Cursor; cursor < window; cursor++ {
		admitInput(t, store, session.ID, fmt.Sprintf("legacy-input-%d", cursor), "legacy input")
	}
	command := &commandpb.Command{CommandId: "packed-admission", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "packed input"},
	}}
	admitted, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{"frame": strings.Repeat("evidence payload shared across events ", 256)}
	const boundaryOperation = "packed-operation"
	record := func(writer *Store) error {
		return writer.ProtocolEvent(t.Context(), owner, boundaryOperation, eventpb.ExecutionProgress_KIND_EXECUTOR,
			eventpb.ExecutionProgress_PHASE_RECEIVED, payload)
	}
	for range window/2 - 1 {
		if err := record(store); err != nil {
			t.Fatal(err)
		}
	}
	first, last := uint64(window-2), uint64(2*window-1)
	expected, err := journalFrames(t.Context(), store.DB(), session.ID, first, last)
	if err != nil || len(expected) != int(last-first+1) {
		t.Fatalf("standalone frames=%d err=%v", len(expected), err)
	}
	writer := New(store.DB(), store.operationTimeout, &settings.Settings{EventBlockWrites: true})
	migrationExec(t, store.DB(), `ALTER TABLE sampler_event_block ADD CONSTRAINT reject_event_block CHECK (false)`)
	if err := record(writer); err == nil {
		t.Fatal("evidence append committed despite a failed block insert")
	}
	current, err := store.GetSession(t.Context(), session.ID, session.OwnerKey)
	if err != nil || current.Cursor != last {
		t.Fatalf("failed block insert advanced Session: %v err=%v", current, err)
	}
	migrationExec(t, store.DB(), `ALTER TABLE sampler_event_block DROP CONSTRAINT reject_event_block`)
	if err := record(writer); err != nil {
		t.Fatal(err)
	}
	var blockStart, blockEnd uint64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT first_sequence,last_sequence FROM sampler_event_block WHERE session_id=$1`,
		session.ID).Scan(&blockStart, &blockEnd); err != nil {
		t.Fatal(err)
	}
	if blockStart != window+1 || blockEnd != 2*window {
		t.Fatalf("sealed window=%d–%d", blockStart, blockEnd)
	}
	// Revert to the disabled writer and read the packed and standalone parts through it.
	if err := record(store); err != nil {
		t.Fatal(err)
	}
	actual, err := journalFrames(t.Context(), store.DB(), session.ID, first, last)
	if err != nil || len(actual) != len(expected) {
		t.Fatalf("mixed frames=%d expected=%d err=%v", len(actual), len(expected), err)
	}
	for i, want := range expected {
		wantWire, err := eventcodec.Uncompress(want.wire)
		if err != nil {
			t.Fatal(err)
		}
		gotWire, err := eventcodec.Uncompress(actual[i].wire)
		if err != nil || actual[i].sequence != want.sequence || !bytes.Equal(gotWire, wantWire) {
			t.Fatalf("frame %d changed after sealing: %v", want.sequence, err)
		}
	}
	// This page begins in the middle of a block and ends in the standalone tail.
	page, err := store.Events(t.Context(), session.ID, 2*window-3, 5)
	if err != nil || len(page) != 5 {
		t.Fatalf("mixed page len=%d err=%v", len(page), err)
	}
	for i, entry := range page {
		if want := uint64(2*window - 2 + i); entry.GetCursor() != want || entry.GetOrigin().GetSequence() != want {
			t.Fatalf("mixed page cursor=%d origin=%d want=%d", entry.GetCursor(), entry.GetOrigin().GetSequence(), want)
		}
	}
	if boundary, progress := page[2], page[3]; boundary.GetEvent().GetBoundaryRecord().GetCorrelationId() != boundaryOperation ||
		len(progress.GetEvent().GetSourceSequences()) != 1 || progress.GetEvent().GetSourceSequences()[0] != boundary.GetCursor() {
		t.Fatalf("boundary and progress link changed across the block boundary: %v %v", boundary, progress)
	}
	gotCommand, err := store.GetCommand(t.Context(), session.ID, command.GetCommandId())
	if err != nil || !proto.Equal(gotCommand.GetAdmission(), admitted) {
		t.Fatalf("packed GetCommand admission=%v err=%v", gotCommand, err)
	}
	gotAdmission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil || !proto.Equal(gotAdmission, admitted) {
		t.Fatalf("packed admission replay=%v err=%v", gotAdmission, err)
	}
}
