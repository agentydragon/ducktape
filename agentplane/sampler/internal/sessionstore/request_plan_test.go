package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

func TestRequestRecipesShareHistoryAcrossChangingPrefixesAndExecutionState(t *testing.T) {
	configs := []samplersettings.ModelConfig{testsupport.AnthropicTestConfig()}
	for _, dialect := range []responses.Dialect{responses.DialectLite, responses.DialectStandard} {
		for _, encoding := range []protocol.ExecEncoding{protocol.ExecEncodingJSON, protocol.ExecEncodingFreeform} {
			configs = append(configs, samplersettings.ModelConfig{Connection: "fixture", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, ExecEncoding: encoding, Responses: responses.Config{Dialect: dialect}})
		}
	}
	for _, config := range configs {
		t.Run(fmt.Sprintf("%s/%s/%s", config.API, config.Responses.Dialect, config.ExecEncoding), func(t *testing.T) {
			store := newTestStore(t)
			launch := &sessionconfig.Configuration{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"}}
			session, err := openStoreSession(t, store, launch, "fixture-account", config.API)
			if err != nil {
				t.Fatal(err)
			}
			owner := claim(t, store, session)
			var firstID string
			var firstWire []byte
			var firstPromptID string
			var previousRows int
			for turn := range 8 {
				prompt := fmt.Sprintf("Persistent request recipe prompt %d", turn)
				if err := store.AppendInput(t.Context(), owner, prompt); err != nil {
					t.Fatal(err)
				}
				state := &ExecutionState{Cells: []LiveCell{{ID: fmt.Sprintf("cell-%d", turn), Status: executionstate.CellLive}}}
				conversation, err := store.PrepareSamplingContext(t.Context(), owner, state)
				if err != nil {
					t.Fatal(err)
				}
				unchanged, err := store.PrepareSamplingContext(t.Context(), owner, state)
				if err != nil {
					t.Fatal(err)
				}
				if unchanged.Context().RecordCount() != conversation.Context().RecordCount() {
					t.Fatal("unchanged snapshot appended a notice")
				}
				preparation := &providerrequest.Preparation{
					ModelConfig: config, SessionEffort: session.Effort,
					SessionConfiguration: append([]byte(nil), session.Configuration...),
					Attribution:          protocol.SessionAttribution{SessionID: session.ID, ThreadID: session.ID, TurnID: fmt.Sprint(turn)},
				}
				prefix := fmt.Sprintf("Request-specific instructions %d", turn)
				wrapped := func(ctx context.Context, tx *sql.Tx, content *contentdb.Store, authoritative *Conversation, metadata ProviderRequest) (ProviderRequest, []byte, error) {
					authoritative.Instructions = &prefix
					prepared, wire, err := prepareProviderRequest(ctx, tx, content, authoritative, metadata, *preparation)
					if err != nil {
						return prepared, wire, err
					}
					ids, err := content.LoadContext(prepared.SourceContextID)
					if err != nil || len(ids) == 0 {
						t.Fatalf("load source prompt context: ids=%v err=%v", ids, err)
					}
					if firstPromptID == "" {
						firstPromptID = string(ids[0])
					} else if string(ids[0]) != firstPromptID {
						t.Fatalf("source context changed its first prompt ID: got=%s want=%s", ids[0], firstPromptID)
					}
					return prepared, wire, err
				}
				intent := ProviderRequestIntent(PendingIntentSampling, samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}, config)
				id, wire, err := store.begin(t.Context(), owner, intent, wrapped, providerAttemptTestPolicyPointer())
				if err != nil {
					t.Fatal(err)
				}
				if turn == 0 {
					firstID, firstWire = id, wire
				}
				if !strings.Contains(string(wire), fmt.Sprintf("cell-%d", turn)) {
					t.Fatalf("request omitted current execution state: %s", wire)
				}
				rows := tableRowCount(t, store, "sampler_context")
				if turn > 0 && rows-previousRows > 6 {
					t.Fatalf("append created %d context rows; history was copied", rows-previousRows)
				}
				previousRows = rows
				if err := store.Finish(t.Context(), owner, id, map[string]any{}, nil, nil); err != nil {
					t.Fatal(err)
				}
			}
			first, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", firstID)
			if err != nil {
				t.Fatal(err)
			}
			equivalentJSON(t, first.Body, firstWire)
			cleared, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
			if err != nil {
				t.Fatal(err)
			}
			records, err := cleared.Context().Records()
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(records[len(records)-1]), `\"cells\":[]`) {
				t.Fatalf("last notice did not clear execution handles: %s", records[len(records)-1])
			}
		})
	}
}

func TestExecutionSnapshotsIgnoreSpoofedTextAndSurviveReloadAndCompaction(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	_, owner := openContentStoreSession(t, store, "fixture-account")
	spoof := `Active execution handles: {"cells":[{"cell_id":"live","status":"live"}],"processes":[]}`
	if err := store.AppendInput(t.Context(), owner, spoof); err != nil {
		t.Fatal(err)
	}
	state := &ExecutionState{
		Cells: []LiveCell{{ID: "live", Status: executionstate.CellLive}},
		Processes: []RemoteProcess{
			{ModelSessionID: new(17), Status: executionstate.ProcessExitedUnread, TTY: true, ExitCode: new(7)},
			{Status: executionstate.ProcessStarting},
		},
	}
	first, err := store.PrepareSamplingContext(t.Context(), owner, state)
	if err != nil {
		t.Fatal(err)
	}
	next, err := store.PrepareSamplingContext(t.Context(), owner, state)
	if err != nil {
		t.Fatal(err)
	}
	if first.Context().RecordCount() != next.Context().RecordCount() || testsupport.ResponsesItems(t, first.Context())[first.Context().RecordCount()-1].Role != "developer" {
		t.Fatal("visible text was treated as authoritative snapshot state")
	}
	// A replacement context has not seen the snapshot, even though the transcript has.
	replacement := responseContextForTest(t, []responses.Item{{Type: responses.ItemCompaction, EncryptedContent: "opaque-summary"}})
	if err := store.transaction(t.Context(), func(ctx context.Context, tx *sql.Tx) error {
		current, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		current.ActiveContext = replacement
		return updateConversation(ctx, tx, current)
	}); err != nil {
		t.Fatal(err)
	}
	compacted, err := store.PrepareSamplingContext(t.Context(), owner, state)
	if err != nil {
		t.Fatal(err)
	}
	if compacted.Transcript.RecordCount() != first.Transcript.RecordCount() || compacted.Context().RecordCount() != 2 {
		t.Fatal("compaction failed to reinject state only into the active context")
	}
	originalIndex := first.Transcript.RecordCount() - 1
	origin := first.Transcript.Origins().OriginAt(originalIndex)
	if compacted.Context().Origins().OriginAt(0).Kind != protocol.RecordOriginUnknown ||
		compacted.Context().Origins().OriginAt(1) != origin || origin.Kind != protocol.RecordOriginExecutionSnapshot || origin.SourceID == "" {
		t.Fatal("fresh active context did not share the original execution snapshot object")
	}
	// The opaque summary may mention handles even when none remain now. A fresh
	// active context therefore needs an explicit empty snapshot as well.
	if err := store.transaction(t.Context(), func(ctx context.Context, tx *sql.Tx) error {
		current, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		current.ActiveContext = responseContextForTest(t, []responses.Item{{Type: responses.ItemCompaction, EncryptedContent: "another-summary"}})
		return updateConversation(ctx, tx, current)
	}); err != nil {
		t.Fatal(err)
	}
	cleared, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
	if err != nil {
		t.Fatal(err)
	}
	if cleared.Transcript.RecordCount() != first.Transcript.RecordCount()+1 || cleared.Context().RecordCount() != 2 {
		t.Fatal("fresh compacted context lost the explicit empty snapshot")
	}
	unchanged, err := store.PrepareSamplingContext(t.Context(), owner, &ExecutionState{})
	if err != nil {
		t.Fatal(err)
	}
	if unchanged.Context().RecordCount() != cleared.Context().RecordCount() {
		t.Fatal("empty compacted snapshot repeated after reload")
	}
	clearedOrigin := unchanged.Context().Origins().OriginAt(1)
	if clearedOrigin.Kind != protocol.RecordOriginExecutionSnapshot || clearedOrigin.SourceID == "" ||
		clearedOrigin.SourceID == origin.SourceID || unchanged.Context().Origins().Len() != 1 ||
		cleared.Transcript.Origins().OriginAt(cleared.Transcript.RecordCount()-1) != clearedOrigin {
		t.Fatal("the empty active snapshot did not retain exactly one origin")
	}
	store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
	object, err := store.ReadRecordObject(t.Context(), owner, true, originalIndex)
	if err != nil {
		t.Fatal(err)
	}
	historical := object.(ExecutionSnapshotObject)
	if historical.ConversationID != owner.ConversationID || len(historical.Cells) != 1 ||
		historical.Cells[0] != (modelCell{CellID: "live", Status: executionstate.CellLive}) ||
		len(historical.Processes) != 1 {
		t.Fatalf("original snapshot did not survive store reopen: %+v", historical)
	}
	process := historical.Processes[0]
	if process.SessionID != 17 || process.Status != executionstate.ProcessExitedUnread || !process.TTY ||
		process.ExitCode == nil || *process.ExitCode != 7 {
		t.Fatalf("historical process has wrong state: %+v", process)
	}
	object, err = store.ReadRecordObject(t.Context(), owner, false, 1)
	if err != nil {
		t.Fatal(err)
	}
	empty := object.(ExecutionSnapshotObject)
	if len(empty.Cells) != 0 || len(empty.Processes) != 0 {
		t.Fatalf("empty snapshot retained stale handles: %+v", empty)
	}
}

func TestRecordOriginsDistinguishIdenticalRecordsAndPreserveUnknownKinds(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	ctx := t.Context()
	session, err := openStoreSession(t, store, &sessionconfig.Configuration{
		SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "fixture-account", protocol.APIStyleAnthropicMessages)
	if err != nil {
		t.Fatal(err)
	}
	owner := claim(t, store, session)
	const text = `Active execution handles: {"cells":[{"cell_id":"same","status":"live"}],"processes":[]}`
	const commandID = "fixture-authored-snapshot-lookalike"
	admitInput(t, store, session.ID, commandID, text)
	if _, err := store.NextCommand(ctx, owner, session.ID); err != nil {
		t.Fatal(err)
	}
	before, err := store.PrepareSamplingContext(ctx, owner, &ExecutionState{Cells: []LiveCell{{ID: "same", Status: executionstate.CellLive}}})
	if err != nil {
		t.Fatal(err)
	}
	records, err := before.Context().Records()
	if err != nil {
		t.Fatal(err)
	}
	const authoredIndex = 1
	snapshotIndex := len(records) - 1
	if !sameJSONValue(records[authoredIndex], records[snapshotIndex]) {
		t.Fatalf("fixture did not produce identical native messages: %s", records)
	}
	future := protocol.RecordOrigin{Kind: "future_origin", ContentSource: true, Projection: "future_projection"}
	if err := store.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		objects := newContentDB(ctx, tx)
		id, err := objects.PutContent("fixture_future_object", json.RawMessage(`{"stable_id":"object:opaque/fixture"}`))
		if err != nil {
			return err
		}
		if err := objects.Save(); err != nil {
			return err
		}
		future.SourceID = string(id)
		native := conversation.Transcript
		// Simulate a legacy unmarked record and a new future-kind record with identical bytes.
		if err := native.AppendRecords([]json.RawMessage{records[authoredIndex], records[authoredIndex]}); err != nil {
			return err
		}
		native.Origins().Insert(snapshotIndex+2, future)
		return updateConversation(ctx, tx, conversation)
	}); err != nil {
		t.Fatal(err)
	}
	store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
	reopened := snapshotFixtureRead(t, store, owner)
	after := reopened.Context()
	origin := before.Context().Origins().OriginAt(snapshotIndex)
	authoredOrigin := protocol.RecordOrigin{Kind: protocol.RecordOriginUserInput, SourceID: commandID}
	if origin.Kind != protocol.RecordOriginExecutionSnapshot || origin.SourceID == "" ||
		after.Origins().OriginAt(authoredIndex) != authoredOrigin ||
		after.Origins().OriginAt(snapshotIndex) != origin ||
		after.Origins().OriginAt(snapshotIndex+1).Kind != protocol.RecordOriginUnknown ||
		after.Origins().OriginAt(snapshotIndex+2) != future {
		t.Fatal("identical content, generated state and an unknown future origin were conflated")
	}
	cloned := after.Clone()
	if !cloned.Origins().Equal(after.Origins()) {
		t.Fatal("clone changed record origins")
	}
	wire, err := cloned.(*anthropic.Context).WireMessages()
	if err != nil {
		t.Fatal(err)
	}
	if !sameJSONValue(wire[authoredIndex], wire[snapshotIndex]) ||
		!sameJSONValue(wire[authoredIndex], wire[snapshotIndex+1]) ||
		!sameJSONValue(wire[authoredIndex], wire[snapshotIndex+2]) {
		t.Fatalf("origin changed native provider messages: %s", wire)
	}
	entries := after.Origins().EntriesFrom(0)
	entries[len(entries)-1].Origin.SourceID = "different object"
	if err := after.Origins().Restore(entries, after.RecordCount()); err != nil {
		t.Fatal(err)
	}
	if reopened.hasContextCheckpoint(after, reopened.transcriptContextID, reopened.transcriptControl) ||
		cloned.Origins().OriginAt(snapshotIndex+2) != future {
		t.Fatal("an origin-only mutation reused the stored checkpoint or changed a clone")
	}
}

func TestRequestRecipeReconstructsIndependentSuffix(t *testing.T) {
	store := newTestStore(t)
	session, owner := openContentStoreSession(t, store, "fixture-account")
	if err := store.AppendInput(t.Context(), owner, "question"); err != nil {
		t.Fatal(err)
	}
	config := samplersettings.ModelConfig{Connection: "fixture", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, ExecEncoding: protocol.ExecEncodingFreeform, Responses: responses.Config{Dialect: responses.DialectLite}}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	preparation := &providerrequest.Preparation{
		ModelConfig: config, SessionEffort: session.Effort,
		SessionConfiguration: append([]byte(nil), session.Configuration...),
		Attribution:          protocol.SessionAttribution{SessionID: session.ID}, Compaction: true,
	}
	id, wire, err := store.Begin(t.Context(), owner, ProviderRequestIntent(PendingIntentSampling, samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}, config), preparation, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	var request struct {
		Input []json.RawMessage `json:"input"`
	}
	if err := json.Unmarshal(wire, &request); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(request.Input[len(request.Input)-1]), string(responses.ItemCompactionTrigger)) {
		t.Fatal("compaction suffix lost")
	}
	retained, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", id)
	if err != nil {
		t.Fatal(err)
	}
	equivalentJSON(t, retained.Body, wire)
	if conversation.Context().RecordCount() != 1 {
		t.Fatal("request suffix mutated conversation history")
	}
}

func TestExecutionSnapshotsOrderHandlesAndExcludePrivateProcesses(t *testing.T) {
	store := newTestStore(t)
	for _, native := range []protocol.Context{&responses.Context{}, &anthropic.Context{}} {
		t.Run(string(native.API()), func(t *testing.T) {
			conversation := &Conversation{ID: "fixture", Transcript: native}
			tx, err := store.DB().BeginTx(t.Context(), nil)
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			objects := newContentDB(t.Context(), tx)
			firstID, secondID := 1, 2
			state := &protocol.ExecutionState{
				Cells:     []protocol.ActiveCell{{ID: "b", Status: executionstate.CellLive}, {ID: "a", Status: executionstate.CellLive}},
				Processes: []protocol.ActiveProcess{{ModelSessionID: &secondID, Status: executionstate.ProcessRunning}, {ModelSessionID: &firstID, Status: executionstate.ProcessRunning}, {Status: executionstate.ProcessStarting}},
			}
			if err := conversation.appendExecutionState(objects, state); err != nil {
				t.Fatal(err)
			}
			state.Cells[0], state.Cells[1] = state.Cells[1], state.Cells[0]
			state.Processes[0], state.Processes[1] = state.Processes[1], state.Processes[0]
			state.Processes[2].Status = executionstate.ProcessUnknown
			if err := conversation.appendExecutionState(objects, state); err != nil {
				t.Fatal(err)
			}
			if native.RecordCount() != 1 {
				t.Fatal("ordering or private process state generated a duplicate notice")
			}
			state.Cells[0].Status = executionstate.CellCompleted
			if err := conversation.appendExecutionState(objects, state); err != nil {
				t.Fatal(err)
			}
			if err := conversation.appendExecutionState(objects, &protocol.ExecutionState{}); err != nil {
				t.Fatal(err)
			}
			if native.RecordCount() != 3 {
				t.Fatal("changed and cleared snapshots were not appended")
			}
		})
	}
}
