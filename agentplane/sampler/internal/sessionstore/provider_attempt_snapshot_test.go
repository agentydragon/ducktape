package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const snapshotFixtureReply = `{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Snapshot fixture reply"}]}`

func installSnapshotFixtureCompaction(t *testing.T, store *Store, owner Owner) {
	t.Helper()
	source, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	intentID, _, err := store.BeginCompaction(t.Context(), owner, IntentPayload{Kind: PendingIntentCompaction}, source.Context(), nil, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	replacement := responseContextForTest(t, []responses.Item{{Type: responses.ItemCompaction, EncryptedContent: "fixture-opaque-summary"}})
	if err := store.FinishCompaction(t.Context(), owner, intentID, map[string]string{"status": "completed"}, source.Context(), replacement, nil); err != nil {
		t.Fatal(err)
	}
}

func beginSnapshotFixture(t *testing.T, store *Store, owner Owner, source *Conversation) (string, []byte, *ProviderAttemptSnapshot) {
	t.Helper()
	request, preparation := providerAttemptTestPreparation(PendingIntentSampling)
	id, wire, snapshot, err := store.BeginPreparedSampling(t.Context(), owner, source, request, preparation, providerAttemptTestPolicy())
	if err != nil || id == "" || len(wire) == 0 || snapshot == nil {
		t.Fatalf("begin prepared sampling: id=%q wire=%d bytes snapshot=%v err=%v", id, len(wire), snapshot != nil, err)
	}
	return id, wire, snapshot
}

func snapshotFixtureRead(t *testing.T, store *Store, owner Owner) *Conversation {
	t.Helper()
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	return conversation
}

func snapshotFixtureCursor(t *testing.T, store *Store, sessionID string) uint64 {
	t.Helper()
	session, err := store.GetSession(t.Context(), sessionID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	return session.Cursor
}

func finishSnapshotFixture(t *testing.T, store *Store, owner Owner, id string, snapshot *ProviderAttemptSnapshot, tokens *int, validate func(protocol.Context) error) error {
	t.Helper()
	return store.FinishPreparedSampling(t.Context(), owner, id, snapshot, map[string]string{"status": "completed"},
		[]json.RawMessage{json.RawMessage(snapshotFixtureReply)}, tokens, validate)
}

func TestPreparedSamplingParity(t *testing.T) {
	for _, name := range []string{"transcript", "compacted"} {
		t.Run(name, func(t *testing.T) {
			type sampling struct {
				wire         []byte
				conversation *Conversation
				events       []*eventlogpb.EventEntry
			}
			run := func(prepared bool) sampling {
				store := newTestStore(t)
				store.now = func() time.Time { return time.Date(2026, 1, 7, 12, 0, 0, 0, time.UTC) }
				// The platform instructions contain the Session ID, so reuse it in the two isolated databases.
				session, err := openStoreSession(t, store, &sessionconfig.Configuration{
					SessionId: "91000000-0000-4000-8000-000000000001",
					Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
				}, "fixture-account", protocol.APIStyleResponses)
				if err != nil {
					t.Fatal(err)
				}
				owner := claim(t, store, session)
				if err := store.AppendInput(t.Context(), owner, "Fixture prior input"); err != nil {
					t.Fatal(err)
				}
				if name == "compacted" {
					installSnapshotFixtureCompaction(t, store, owner)
				}
				admitInput(t, store, session.ID, "fixture-snapshot-command", "Fixture new input")
				if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
					t.Fatal(err)
				}
				shell := "/fixture/bash"
				if err := store.SetExecutorSession(t.Context(), owner, "fixture-executor-session", &shell); err != nil {
					t.Fatal(err)
				}
				if err := store.CellResult(t.Context(), owner, "fixture-live-cell", executionstate.CellLive); err != nil {
					t.Fatal(err)
				}
				before := snapshotFixtureCursor(t, store, session.ID)
				request, preparation := providerAttemptTestPreparation(PendingIntentSampling)
				var snapshot *ProviderAttemptSnapshot
				var id string
				var wire []byte
				if prepared {
					source := snapshotFixtureRead(t, store, owner)
					id, wire, snapshot, err = store.BeginPreparedSampling(t.Context(), owner, source, request, preparation, providerAttemptTestPolicy())
				} else {
					if _, err := store.PrepareSamplingContext(t.Context(), owner); err != nil {
						t.Fatal(err)
					}
					id, wire, err = store.Begin(t.Context(), owner, request, preparation, providerAttemptTestPolicyPointer())
				}
				if err != nil {
					t.Fatal(err)
				}
				retained, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", id)
				if err != nil || !bytes.Equal(wire, retained.Body) {
					t.Fatalf("retained request parity: err=%v", err)
				}
				// Evidence advances the Session's journal cursor while the snapshot is in use.
				evidence := &ResponseEvidence{Store: store, Owner: owner, IntentID: id, ProgressLease: time.Minute}
				observation := &eventpb.Event{Observation: &eventpb.Event_TextDelta{
					TextDelta: &eventpb.TextDelta{ItemId: "fixture-item", Text: "sampled text"},
				}}
				if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("fixture provider frame")}, true, observation); err != nil {
					t.Fatal(err)
				}
				tokens := 42
				if prepared {
					err = finishSnapshotFixture(t, store, owner, id, snapshot, &tokens, protocol.Context.CheckSettled)
				} else {
					err = store.Finish(t.Context(), owner, id, map[string]string{"status": "completed"},
						[]json.RawMessage{json.RawMessage(snapshotFixtureReply)}, &tokens)
				}
				if err != nil {
					t.Fatal(err)
				}
				conversation := snapshotFixtureRead(t, store, owner)
				if conversation.Pending != nil || conversation.ContextTokens == nil || *conversation.ContextTokens != tokens {
					t.Fatalf("settled conversation: pending=%+v tokens=%v", conversation.Pending, conversation.ContextTokens)
				}
				events, err := store.Events(t.Context(), session.ID, before, 32)
				if err != nil {
					t.Fatal(err)
				}
				if len(events) != 7 || snapshotFixtureCursor(t, store, session.ID) != before+7 {
					t.Fatalf("journal after sampling has %d events, want dispatch, evidence and finish", len(events))
				}
				normalizeSnapshotFixtureEvents(events, id)
				return sampling{wire: wire, conversation: conversation, events: events}
			}
			old, prepared := run(false), run(true)
			if !bytes.Equal(old.wire, prepared.wire) {
				for i := 0; i < min(len(old.wire), len(prepared.wire)); i++ {
					if old.wire[i] != prepared.wire[i] {
						t.Fatalf("prepared request diverged at byte %d", i)
					}
				}
				t.Fatalf("request byte counts differ: old=%d prepared=%d", len(old.wire), len(prepared.wire))
			}
			if bytes.Contains(prepared.wire, []byte("fixture-live-cell")) || !bytes.Contains(prepared.wire, []byte("/fixture/bash")) {
				t.Fatal("prepared request added a live-handle notice or omitted the environment update")
			}
			if !sameNativeContext(old.conversation.Transcript, prepared.conversation.Transcript) ||
				!sameNativeContext(old.conversation.Context(), prepared.conversation.Context()) ||
				(name == "compacted") != (prepared.conversation.ActiveContext != nil) {
				t.Fatal("prepared finish changed retained transcript or active context")
			}
			for i, want := range old.events {
				if got := prepared.events[i]; !proto.Equal(got, want) {
					t.Fatalf("journal event %d differs: got %v, want %v", i, got, want)
				}
			}
		})
	}
}

func normalizeSnapshotFixtureEvents(events []*eventlogpb.EventEntry, intentID string) {
	for _, entry := range events {
		entry.Origin.SourceId = ""
		event := entry.GetEvent()
		event.At = nil
		if boundary := event.GetBoundaryRecord(); boundary != nil {
			boundary.RecordId = ""
			if boundary.CorrelationId == intentID {
				boundary.CorrelationId = "fixture-attempt"
			}
			boundary.Payload = bytes.ReplaceAll(boundary.Payload, []byte(intentID), []byte("fixture-attempt"))
		}
		if progress := event.GetExecutionProgress(); progress != nil && progress.OperationId == intentID {
			progress.OperationId = "fixture-attempt"
		}
	}
}

func TestPreparedSamplingReusesRetainedTranscript(t *testing.T) {
	store, session, owner := claimedSession(t)
	if err := store.AppendInput(t.Context(), owner, "Fixture input retained outside the active context"); err != nil {
		t.Fatal(err)
	}
	transcriptOnlyHead := snapshotFixtureRead(t, store, owner).transcriptContextID
	installSnapshotFixtureCompaction(t, store, owner)
	admitProviderCommand(t, store, owner, session.ID, "fixture-snapshot-reuse", false)
	source := snapshotFixtureRead(t, store, owner)
	var contentID string
	var payload []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT b.id,b.payload FROM sampler_content b
		JOIN sampler_context c ON c.content_id=b.id WHERE c.id=$1`, transcriptOnlyHead).Scan(&contentID, &payload); err != nil {
		t.Fatal(err)
	}
	// Make a transcript-only blob unreadable after Read. Rendering the compacted request
	// still has its own blobs available, while a full native-context reload would fail.
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_content SET payload=$2 WHERE id=$1`, contentID, []byte("unreadable")); err != nil {
		t.Fatal(err)
	}
	id, wire, snapshot := beginSnapshotFixture(t, store, owner, source)
	if !bytes.Contains(wire, []byte("fixture-opaque-summary")) {
		t.Fatal("request did not use the compacted active context")
	}
	if err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, protocol.Context.CheckSettled); err != nil {
		t.Fatalf("finish reloaded the retained transcript: %v", err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_content SET payload=$2 WHERE id=$1`, contentID, payload); err != nil {
		t.Fatal(err)
	}
	conversation := snapshotFixtureRead(t, store, owner)
	if conversation.Transcript.RecordCount() != source.Transcript.RecordCount()+1 ||
		conversation.Context().RecordCount() != source.Context().RecordCount()+1 {
		t.Fatal("finish did not extend both retained contexts by one reply")
	}
}

func TestPreparedSamplingBeginReloadsChangedContext(t *testing.T) {
	store, session, owner := claimedSession(t)
	admitProviderCommand(t, store, owner, session.ID, "fixture-stale-begin", false)
	source := snapshotFixtureRead(t, store, owner)
	const newInput = "Fixture input appended after Read"
	if err := store.AppendInput(t.Context(), owner, newInput); err != nil {
		t.Fatal(err)
	}
	id, wire, snapshot := beginSnapshotFixture(t, store, owner, source)
	if !bytes.Contains(wire, []byte(newInput)) {
		t.Fatal("request omitted context committed after Read")
	}
	if err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, protocol.Context.CheckSettled); err != nil {
		t.Fatal(err)
	}
	recorded := snapshotFixtureRead(t, store, owner)
	if recorded.Transcript.RecordCount() != source.Transcript.RecordCount()+2 {
		t.Fatal("finish failed to retain both intervening input and appended reply")
	}
}

func TestPreparedSamplingFinishReloadsChangedContext(t *testing.T) {
	for _, change := range []string{"head", "control", "origins"} {
		t.Run(change, func(t *testing.T) {
			store, session, owner := claimedSession(t)
			if change == "control" {
				installSnapshotFixtureCompaction(t, store, owner)
			}
			admitProviderCommand(t, store, owner, session.ID, "fixture-stale-finish", false)
			id, _, snapshot := beginSnapshotFixture(t, store, owner, snapshotFixtureRead(t, store, owner))
			const intervening = "Fixture context committed after dispatch"
			// Exercise a persisted context change without disturbing the pending attempt.
			if err := store.transaction(t.Context(), func(ctx context.Context, tx *sql.Tx) error {
				current, err := lockOwner(ctx, tx, owner)
				if err != nil {
					return err
				}
				switch change {
				case "head":
					if err := current.Transcript.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: intervening}}, protocol.RecordOrigin{}); err != nil {
						return err
					}
				case "control":
					current.executionSnapshots[current.ActiveContext] = &modelExecutionSnapshot{
						Cells: []modelCell{{CellID: "fixture-intervening-cell", Status: executionstate.CellLive}}, Processes: []modelProcess{},
					}
				case "origins":
					records, err := current.Transcript.Records()
					if err != nil {
						return err
					}
					bare, err := newNativeContext(current.API)
					if err != nil {
						return err
					}
					if err := bare.AppendRecords(records); err != nil {
						return err
					}
					current.executionSnapshots[bare] = current.executionSnapshots[current.Transcript]
					current.Transcript = bare
				}
				return store.updateConversation(ctx, tx, current)
			}); err != nil {
				t.Fatal(err)
			}
			before := snapshotFixtureRead(t, store, owner)
			validate := func(context protocol.Context) error {
				records, err := context.Records()
				if err != nil {
					return err
				}
				if len(records) != before.Transcript.RecordCount()+1 || !bytes.Contains(records[len(records)-1], []byte("Snapshot fixture reply")) {
					return errors.New("validation did not see the resulting transcript")
				}
				if change == "head" && !bytes.Contains(records[len(records)-2], []byte(intervening)) {
					return errors.New("validation omitted context committed after dispatch")
				}
				return context.CheckSettled()
			}
			if err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, validate); err != nil {
				t.Fatal(err)
			}
			after := snapshotFixtureRead(t, store, owner)
			records, err := after.Transcript.Records()
			if err != nil {
				t.Fatal(err)
			}
			if len(records) != before.Transcript.RecordCount()+1 {
				t.Fatalf("durable transcript has %d records, want %d", len(records), before.Transcript.RecordCount()+1)
			}
			if change == "control" && !reflect.DeepEqual(before.executionSnapshots[before.ActiveContext], after.executionSnapshots[after.ActiveContext]) {
				t.Fatal("finish replaced the current context control with the snapshot control")
			}
			providerOrigin := protocol.RecordOrigin{Kind: protocol.RecordOriginProviderResult, SourceID: id}
			if change == "origins" && (after.Transcript.OriginAt(0).Kind != protocol.RecordOriginUnknown || after.Transcript.OriginAt(after.Transcript.RecordCount()-1) != providerOrigin) {
				t.Fatal("finish restored an origin root from the stale snapshot")
			}
		})
	}
}

func TestPreparedSamplingFinishFences(t *testing.T) {
	for _, why := range []string{"interrupt", "owner changed", "attempt expired", "wrong attempt"} {
		t.Run(why, func(t *testing.T) {
			store, session, owner := claimedSession(t)
			admitProviderCommand(t, store, owner, session.ID, "fixture-fenced-finish", false)
			id, _, snapshot := beginSnapshotFixture(t, store, owner, snapshotFixtureRead(t, store, owner))
			switch why {
			case "interrupt":
				if _, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation); err != nil {
					t.Fatal(err)
				}
			case "owner changed":
				expireLease(t, store, session)
				claim(t, store, session)
			case "attempt expired":
				if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent
					SET provider_attempt_lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, id); err != nil {
					t.Fatal(err)
				}
			case "wrong attempt":
				if err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, protocol.Context.CheckSettled); err != nil {
					t.Fatal(err)
				}
				id, _, _ = beginSnapshotFixture(t, store, owner, snapshotFixtureRead(t, store, owner))
			}
			cursor := snapshotFixtureCursor(t, store, session.ID)
			err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, protocol.Context.CheckSettled)
			switch why {
			case "interrupt":
				if !errors.As(err, new(InterruptPendingError)) {
					t.Fatalf("finish returned %v, want interrupt fence", err)
				}
			case "owner changed":
				if !errors.As(err, new(*LostOwnershipError)) {
					t.Fatalf("finish returned %v, want ownership fence", err)
				}
			default:
				if !errors.Is(err, protocol.ErrPrecondition) {
					t.Fatalf("finish returned %v, want attempt fence", err)
				}
			}
			assertOpenSnapshotFixture(t, store, session.ID, id, cursor)
		})
	}
}

func assertOpenSnapshotFixture(t *testing.T, store *Store, sessionID, id string, cursor uint64) {
	t.Helper()
	var open bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result IS NULL FROM sampler_intent WHERE id=$1`, id).Scan(&open); err != nil {
		t.Fatal(err)
	}
	if got := snapshotFixtureCursor(t, store, sessionID); !open || got != cursor {
		t.Fatalf("failed finish changed durable state: open=%v, cursor=%d, want %d", open, got, cursor)
	}
}

func TestPreparedSamplingFinishRetriesAfterRollback(t *testing.T) {
	store, session, owner := claimedSession(t)
	admitProviderCommand(t, store, owner, session.ID, "fixture-rollback-finish", false)
	id, _, snapshot := beginSnapshotFixture(t, store, owner, snapshotFixtureRead(t, store, owner))
	before := snapshotFixtureRead(t, store, owner)
	cursor := snapshotFixtureCursor(t, store, session.ID)

	invalid := errors.New("fixture rejected continuation")
	err := finishSnapshotFixture(t, store, owner, id, snapshot, nil, func(protocol.Context) error { return invalid })
	if !errors.As(err, new(*InvalidSamplingContinuationError)) || !errors.Is(err, invalid) {
		t.Fatalf("finish returned %v, want typed invalid continuation", err)
	}
	assertOpenSnapshotFixture(t, store, session.ID, id, cursor)

	// Reject the conversation update in PostgreSQL. The attempt and result evidence
	// fields must roll back even though finalization already produced an in-memory suffix.
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_conversation
		ADD CONSTRAINT fixture_fail_finish CHECK (context_tokens IS DISTINCT FROM 777)`); err != nil {
		t.Fatal(err)
	}
	tokens := 777
	err = finishSnapshotFixture(t, store, owner, id, snapshot, &tokens, protocol.Context.CheckSettled)
	var constraint *pq.Error
	if !errors.As(err, &constraint) || constraint.Constraint != "fixture_fail_finish" {
		t.Fatalf("finish returned %v, want PostgreSQL failure to persist the new context", err)
	}
	assertOpenSnapshotFixture(t, store, session.ID, id, cursor)
	if after := snapshotFixtureRead(t, store, owner); !sameNativeContext(before.Transcript, after.Transcript) {
		t.Fatal("failed commit changed the durable transcript")
	}
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_conversation DROP CONSTRAINT fixture_fail_finish`); err != nil {
		t.Fatal(err)
	}
	if err := finishSnapshotFixture(t, store, owner, id, snapshot, &tokens, protocol.Context.CheckSettled); err != nil {
		t.Fatalf("retry using the same snapshot: %v", err)
	}
	after := snapshotFixtureRead(t, store, owner)
	if after.Pending != nil || after.Transcript.RecordCount() != before.Transcript.RecordCount()+1 ||
		snapshotFixtureCursor(t, store, session.ID) != cursor+2 {
		t.Fatal("retry did not settle exactly one reply and result record")
	}
}
