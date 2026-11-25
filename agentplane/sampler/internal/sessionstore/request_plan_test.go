package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

func TestRequestRecipesShareHistoryAcrossChangingPrefixes(t *testing.T) {
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
				// Seed a pre-upgrade notice, then simulate a live-handle change on
				// every step. Only the historical notice should appear on the wire.
				if turn == 0 {
					seedLegacyExecutionSnapshot(t, store, owner, &ExecutionState{})
				}
				if err := store.CellResult(t.Context(), owner, fmt.Sprintf("cell-%d", turn), executionstate.CellLive); err != nil {
					t.Fatal(err)
				}
				if _, err := store.PrepareSamplingContext(t.Context(), owner); err != nil {
					t.Fatal(err)
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
				if bytes.Count(wire, []byte("Active execution handles: ")) != 1 ||
					bytes.Contains(wire, []byte(fmt.Sprintf("cell-%d", turn))) {
					t.Fatalf("request rewrote the historical notice or added current handle state: %s", wire)
				}
				if config.API == protocol.APIStyleResponses && turn > 0 && !bytes.Contains(wire, []byte("fixture-opaque-reasoning")) {
					t.Fatalf("request dropped earlier encrypted reasoning: %s", wire)
				}
				rows := tableRowCount(t, store, "sampler_context")
				if turn > 0 && rows-previousRows > 6 {
					t.Fatalf("append created %d context rows; history was copied", rows-previousRows)
				}
				previousRows = rows
				var reply []json.RawMessage
				if config.API == protocol.APIStyleResponses && turn == 0 {
					reply = []json.RawMessage{json.RawMessage(`{"type":"reasoning","id":"fixture-reasoning","summary":[],"encrypted_content":"fixture-opaque-reasoning"}`)}
				}
				if err := store.Finish(t.Context(), owner, id, map[string]any{}, reply, nil); err != nil {
					t.Fatal(err)
				}
				if config.API == protocol.APIStyleResponses && turn == 1 {
					// Retain a request that actually sampled both the old notice and
					// encrypted reasoning; verify its exact bytes after later steps.
					firstID, firstWire = id, wire
				}
			}
			first, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", firstID)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(first.Body, firstWire) {
				t.Fatal("replaying an earlier provider request changed its bytes")
			}
		})
	}
}

// Write the native notice and typed control an older sampler would have committed.
func seedLegacyExecutionSnapshot(t *testing.T, store *Store, owner Owner, state *ExecutionState) *Conversation {
	t.Helper()
	if err := store.transaction(t.Context(), func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		objects := newContentDB(ctx, tx)
		if err := conversation.apply(func(native protocol.Context) error {
			return store.appendExecutionSnapshot(objects, conversation, native, state)
		}); err != nil {
			return err
		}
		if err := objects.Save(); err != nil {
			return err
		}
		return store.updateConversation(ctx, tx, conversation)
	}); err != nil {
		t.Fatal(err)
	}
	return snapshotFixtureRead(t, store, owner)
}

func TestChatCompactionPreservesExecutionSnapshotObjectsAcrossRoots(t *testing.T) {
	ctx := t.Context()
	store, dsn := newTestStoreWithDSN(t)
	clock := func() time.Time { return time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC) }
	store.now = clock
	session, err := openStoreSession(t, store, &sessionconfig.Configuration{
		SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "fixture-account", protocol.APIStyleChatCompletions)
	if err != nil {
		t.Fatal(err)
	}
	owner := claim(t, store, session)
	for _, input := range []string{strings.Repeat("old user input ", 200), "Keep this request."} {
		if err := store.AppendInput(ctx, owner, input); err != nil {
			t.Fatal(err)
		}
	}
	live := &ExecutionState{
		Cells: []LiveCell{{ID: "fixture-cell", Status: executionstate.CellLive}},
		Processes: []RemoteProcess{
			{ModelSessionID: new(42), Status: executionstate.ProcessRunning, TTY: true},
			{Status: executionstate.ProcessStarting},
		},
	}
	if _, err := store.PrepareSamplingContext(ctx, owner); err != nil {
		t.Fatal(err)
	}
	before := seedLegacyExecutionSnapshot(t, store, owner, live)
	if err := store.CellResult(ctx, owner, "fixture-cell", executionstate.CellLive); err != nil {
		t.Fatal(err)
	}
	const processID = "fixture-executor-process"
	if err := store.RegisterModelProcess(ctx, owner, processID, 42, true); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(ctx, owner, processID, nil, executionstate.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(ctx, owner, "private-process", false); err != nil {
		t.Fatal(err)
	}
	original := before.Context().(*chatcompletions.Context)
	plan, err := original.PrepareCompaction(chatcompletions.CompactionConfig{
		KeepRecentTokens: new(16),
		Replacement:      chatcompletions.CompactionReplacement{Role: "assistant", Template: "{{.Summary}}"},
	})
	if err != nil {
		t.Fatal(err)
	}
	intent, _, err := store.BeginCompaction(ctx, owner, IntentPayload{Kind: PendingIntentCompaction}, original, nil, providerAttemptTestPolicy())
	if err != nil {
		t.Fatal(err)
	}
	raw := []json.RawMessage{json.RawMessage(`{"role":"assistant","content":"An older handoff can mention fixture-cell."}`)}
	audit := chatcompletions.Audit{FinishReason: "stop"}
	replacement, err := plan.Apply(raw, audit, intent)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.FinishCompaction(ctx, owner, intent, audit, original, replacement, func(candidate protocol.Context) error {
		return plan.CheckReduction(candidate.(*chatcompletions.Context))
	}); err != nil {
		t.Fatal(err)
	}

	type nativeSnapshot struct {
		index int
		text  string
	}
	snapshots := func(native protocol.Context) []nativeSnapshot {
		t.Helper()
		records, err := native.Records()
		if err != nil {
			t.Fatal(err)
		}
		var result []nativeSnapshot
		for index, raw := range records {
			var message struct{ Role, Content string }
			if err := json.Unmarshal(raw, &message); err != nil {
				t.Fatal(err)
			}
			if message.Role == "system" && strings.HasPrefix(message.Content, "Active execution handles: ") {
				result = append(result, nativeSnapshot{index: index, text: message.Content})
			}
		}
		return result
	}
	readSnapshot := func(native protocol.Context, transcript bool, index int) (string, ExecutionSnapshotObject) {
		t.Helper()
		object, err := store.ReadRecordObject(ctx, owner, transcript, index)
		if err != nil {
			t.Fatal(err)
		}
		snapshot, ok := object.(ExecutionSnapshotObject)
		origin := native.OriginAt(index)
		if !ok || snapshot.ConversationID != owner.ConversationID ||
			origin.Kind != protocol.RecordOriginExecutionSnapshot || origin.ObjectID == "" {
			t.Fatalf("invalid execution snapshot association: origin=%+v object=%+v", origin, object)
		}
		return origin.ObjectID, snapshot
	}

	store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
	store.contextSourceWrites = true
	store.now = clock
	compacted := snapshotFixtureRead(t, store, owner)
	const running = `Active execution handles: {"cells":[{"cell_id":"fixture-cell","status":"live"}],"processes":[{"session_id":42,"status":"running","tty":true,"exit_code":null}]}`
	historical := snapshots(compacted.Transcript)
	if len(historical) != 1 || historical[0].text != running ||
		compacted.transcriptContextID != before.transcriptContextID {
		t.Fatal("compaction mutated the transcript or its original native snapshot")
	}
	originalID, snapshot := readSnapshot(compacted.Transcript, true, historical[0].index)
	if len(snapshot.Cells) != 1 || snapshot.Cells[0] != (modelCell{CellID: "fixture-cell", Status: executionstate.CellLive}) ||
		len(snapshot.Processes) != 1 || snapshot.Processes[0] != (modelProcess{SessionID: 42, Status: executionstate.ProcessRunning, TTY: true}) {
		t.Fatalf("stored object changed the structured handles: %+v", snapshot)
	}
	current := snapshots(compacted.Context())
	if len(current) != 1 || current[0].text != running {
		t.Fatal("the replacement did not get a snapshot of the live handles")
	}
	activeID, _ := readSnapshot(compacted.Context(), false, current[0].index)
	if activeID != originalID {
		t.Fatal("unchanged semantic state was split into different objects across native roots")
	}

	if err := store.CellResult(ctx, owner, "fixture-cell", executionstate.CellCompleted); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(ctx, owner, processID, nil, executionstate.ProcessExited, new(0), true); err != nil {
		t.Fatal(err)
	}
	after, err := store.PrepareSamplingContext(ctx, owner)
	if err != nil {
		t.Fatal(err)
	}
	if !sameNativeContext(after.Context(), compacted.Context()) || after.transcriptContextID != compacted.transcriptContextID {
		t.Fatal("ordinary settlement changed the replacement snapshot or rewrote the transcript")
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
	before := seedLegacyExecutionSnapshot(t, store, owner, &ExecutionState{Cells: []LiveCell{{ID: "same", Status: executionstate.CellLive}}})
	records, err := before.Context().Records()
	if err != nil {
		t.Fatal(err)
	}
	const authoredIndex = 1
	snapshotIndex := len(records) - 1
	if !sameJSONValue(records[authoredIndex], records[snapshotIndex]) {
		t.Fatalf("fixture did not produce identical native messages: %s", records)
	}
	future := protocol.RecordOrigin{Kind: "future_origin", Projection: "future_projection"}
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
		future.ObjectID = string(id)
		native := conversation.Transcript
		// Simulate a legacy unmarked record and a new future-kind record with identical bytes.
		if err := native.AppendContextRecords([]protocol.ContextRecord{
			{Native: records[authoredIndex]},
			{Native: records[authoredIndex], Origin: future},
		}); err != nil {
			return err
		}
		return store.updateConversation(ctx, tx, conversation)
	}); err != nil {
		t.Fatal(err)
	}
	store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
	reopened := snapshotFixtureRead(t, store, owner)
	after := reopened.Context()
	origin := before.Context().OriginAt(snapshotIndex)
	authoredOrigin := protocol.RecordOrigin{Kind: protocol.RecordOriginUserInput, SourceID: commandID}
	if origin.Kind != protocol.RecordOriginExecutionSnapshot || origin.ObjectID == "" ||
		after.OriginAt(authoredIndex) != authoredOrigin ||
		after.OriginAt(snapshotIndex) != origin ||
		after.OriginAt(snapshotIndex+1).Kind != protocol.RecordOriginUnknown ||
		after.OriginAt(snapshotIndex+2) != future {
		t.Fatal("identical content, generated state and an unknown future origin were conflated")
	}
	cloned := after.Clone()
	if cloned.OriginAt(snapshotIndex+2) != future || cloned.OriginAt(authoredIndex) != authoredOrigin {
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
}

func TestContextSourceWritesCanRollForwardAndBack(t *testing.T) {
	store, session, owner := claimedSession(t)
	store.contextSourceWrites = false
	admitProviderCommand(t, store, owner, session.ID, "before-source-writes", false)
	legacy := snapshotFixtureRead(t, store, owner)
	for i := range legacy.Transcript.RecordCount() {
		if legacy.Transcript.OriginAt(i).Kind != protocol.RecordOriginUnknown {
			t.Fatal("read-only stage wrote a sourced record")
		}
	}
	if legacy.Transcript.RecordCount() == 0 {
		t.Fatal("fixture did not persist the legacy records")
	}
	store.contextSourceWrites = true
	live := &ExecutionState{Cells: []LiveCell{{ID: "during-source-writes", Status: executionstate.CellLive}}}
	seedLegacyExecutionSnapshot(t, store, owner, live)
	store.contextSourceWrites = false
	seedLegacyExecutionSnapshot(t, store, owner, &ExecutionState{})
	after := snapshotFixtureRead(t, store, owner)
	if after.Transcript.RecordCount() != legacy.Transcript.RecordCount()+2 {
		t.Fatal("mixed context lost a notice")
	}
	index := legacy.Transcript.RecordCount()
	if origin := after.Transcript.OriginAt(index); origin.Kind != protocol.RecordOriginExecutionSnapshot || origin.ObjectID == "" {
		t.Fatalf("reader lost the existing source after disabling writes: %+v", origin)
	}
	if after.Transcript.OriginAt(index+1).Kind != protocol.RecordOriginUnknown {
		t.Fatal("disabled writer kept writing sources")
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
