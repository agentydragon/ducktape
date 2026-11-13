package ingress

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/testing/exportfixture"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func TestSamplerRecoveryProjectsRetainedPriorToolAndOnlyAbandonedItems(t *testing.T) {
	apis := []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages}
	cuts := []string{"no_items", "during_text", "before_terminal"}
	// The before_terminal Responses case exceeds one ExportSession scheduling
	// quantum, so leave room for additional real producer-to-History calls.
	plans := make([]*samplerExportPlan, 0, len(apis)*len(cuts)+2)
	for range len(apis)*len(cuts) + 2 {
		plans = append(plans, newSamplerExportPlan(0, 0, false))
	}
	fixture := newSamplerExportFixture(t, plans...)

	for _, api := range apis {
		for _, cut := range cuts {
			t.Run(string(api)+"/"+cut, func(t *testing.T) {
				session := fixture.openSessionWithAPI(t, &identitypb.Identity{
					Name: fixture.identity.Identity.Name, Uid: string(fixture.identity.Identity.UID),
				}, api)
				const turnID = "fixture-turn"
				scenario, err := fixture.source.StartRecoveryScenario(t.Context(), session, turnID)
				if err != nil {
					t.Fatal(err)
				}
				priorIntent, priorEvidence, err := scenario.BeginSampling(t.Context())
				if err != nil {
					t.Fatal(err)
				}
				prior, err := sampleRecoveryFixture(t, api, "prior", "complete", priorEvidence)
				if err != nil {
					t.Fatal(err)
				}
				if err := priorEvidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("unrelated")},
					&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
						ItemId: "unrelated", Kind: eventpb.ItemKind_ITEM_KIND_REASONING,
					}}}); err != nil {
					t.Fatal(err)
				}
				if err := scenario.FinishSampling(t.Context(), priorIntent, prior.Audit, prior.Records, prior.ContextTokens); err != nil {
					t.Fatal(err)
				}
				calls, err := scenario.PendingToolCalls(t.Context())
				if err != nil || len(calls) != 1 {
					t.Fatalf("prior calls=%v err=%v", calls, err)
				}
				call := calls[0]
				output := []protocol.ContentPart{{Type: "input_text", Text: "Earlier command completed"}}
				if err := scenario.FinishToolCall(t.Context(), call, output); err != nil {
					t.Fatal(err)
				}
				beforeRecords, err := scenario.TranscriptRecords(t.Context())
				if err != nil {
					t.Fatal(err)
				}
				_, evidence, err := scenario.BeginSampling(t.Context())
				if err != nil {
					t.Fatal(err)
				}
				if _, err := sampleRecoveryFixture(t, api, "pending", cut, evidence); err == nil {
					t.Fatal("truncated stream completed")
				}
				if api == protocol.APIStyleResponses && cut == "before_terminal" {
					// More than one reverse page must be read before reaching the item evidence.
					for range 130 {
						if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("tail")}); err != nil {
							t.Fatal(err)
						}
					}
				}
				recovery, err := scenario.RecoverInterruptedTurn(t.Context(), errors.New("provider stream interrupted"), "reviewed stream")
				if err != nil {
					t.Fatal(err)
				}
				for index, entry := range recovery.BeforeEvents {
					if !proto.Equal(entry, recovery.Events[index]) {
						t.Fatalf("recovery changed retained event %d", entry.GetCursor())
					}
				}
				wantAbsent := map[string]bool{}
				if cut != "no_items" {
					if api == protocol.APIStyleResponses {
						wantAbsent["pending-reasoning"], wantAbsent["pending-text"] = true, true
					} else {
						wantAbsent["pending#0"], wantAbsent["pending#1"] = true, true
					}
					if cut == "before_terminal" {
						wantAbsent["pending-call"] = true
					}
				}
				reports := 0
				for _, entry := range recovery.Events {
					if report := entry.GetEvent().GetConversationReconciled(); report != nil {
						reports++
						if report.GetTurnId() != turnID || len(report.GetItems()) != len(wantAbsent) {
							t.Fatalf("unexpected reconciliation: %v", report)
						}
						for _, item := range report.GetItems() {
							if !wantAbsent[item.GetItemId()] || item.GetDisposition() != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT {
								t.Fatalf("unexpected item recovery: %v", item)
							}
						}
					}
				}
				if reports != 1 {
					t.Fatalf("reconciliation count=%d", reports)
				}

				exportSamplerHistoryThrough(t, fixture, session, recovery.Cursor)
				projected := projectSamplerHistory(t, fixture, session, recovery.Cursor)
				for id := range wantAbsent {
					item := projected.items[id]
					if item == nil || item.GetRecovery() != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_ABSENT {
						t.Fatalf("History lost absence annotation for %q: %v", id, item)
					}
				}
				for id, item := range projected.items {
					if !wantAbsent[id] && item.GetRecovery() != eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_UNSPECIFIED {
						t.Fatalf("unrelated History item %q was reconciled: %v", id, item)
					}
				}
				if item := projected.items["prior-call"]; item == nil || item.GetTool() == nil ||
					!item.GetTool().GetSucceeded() || item.GetOutputRef() == nil {
					t.Fatalf("History lost the earlier successful tool result: %v", item)
				} else if body := samplerHistoryPayload(t, fixture, session, item.GetOutputRef()); !strings.Contains(body, "Earlier command completed") {
					t.Fatalf("History prior tool output=%q", body)
				}
				if item := projected.items["pending-call"]; item != nil && (item.GetOutputRef() != nil || item.GetTool() != nil || item.GetText() != nil) {
					t.Fatalf("unexecuted tool acquired a History result: %v", item)
				}

				if err := scenario.ReclaimAfterRecovery(t.Context()); err != nil {
					t.Fatal(err)
				}
				afterRecords, err := scenario.TranscriptRecords(t.Context())
				if err != nil {
					t.Fatal(err)
				}
				if len(afterRecords) != len(beforeRecords)+1 {
					t.Fatalf("recovery committed unexpected provider records: %v", afterRecords)
				}
				for index, record := range beforeRecords {
					if !bytes.Equal(record, afterRecords[index]) {
						t.Fatalf("recovery changed earlier context record %d", index)
					}
				}
				if err := evidence.Record(t.Context(), protocol.WireBytes{Bytes: []byte("late")}); !exportfixture.LostOwnership(err) {
					t.Fatalf("late provider evidence was not fenced: %v", err)
				}
				if err := scenario.Release(t.Context()); err != nil {
					t.Fatal(err)
				}
			})
		}
	}
}

type samplerRecoveryResult struct {
	Audit         any
	Records       []json.RawMessage
	ContextTokens *int
}

func sampleRecoveryFixture(t *testing.T, api protocol.APIStyle, id, cut string, evidence protocol.EvidenceSink) (*samplerRecoveryResult, error) {
	t.Helper()
	var events []any
	if api == protocol.APIStyleResponses {
		if cut != "no_items" {
			items := []responses.Item{
				{Type: responses.ItemReasoning, ID: id + "-reasoning", EncryptedContent: "fixture-opaque", Summary: json.RawMessage(`[{"type":"summary_text","text":"observed reasoning"}]`)},
				{Type: responses.ItemMessage, ID: id + "-text", Role: "assistant", Content: json.RawMessage(`[{"type":"output_text","text":"observed text"}]`)},
				{Type: responses.ItemFunctionCall, ID: id + "-tool", CallID: id + "-call", Name: "exec", Arguments: `{"code":"text(1)"}`},
			}
			for index, item := range items {
				events = append(events, responses.Event{Type: responses.EventOutputItemAdded, Item: &item})
				if index == 1 && cut == "during_text" {
					text := "observed prefix"
					events = append(events, responses.Event{Type: "response.output_text.delta", ItemID: item.ID, Delta: &text})
					break
				}
				events = append(events, responses.Event{Type: responses.EventOutputItemDone, Item: &item})
			}
			if cut == "complete" {
				events = append(events, map[string]any{"type": "response.completed", "response": map[string]any{"id": id, "status": "completed"}})
			}
		}
	} else {
		events = append(events, map[string]any{"type": "message_start", "message": map[string]any{
			"id": id, "role": "assistant", "content": []any{}, "usage": map[string]any{"input_tokens": 1, "output_tokens": 0},
		}})
		if cut != "no_items" {
			blocks := []map[string]any{
				{"type": "thinking", "thinking": "observed reasoning", "signature": "fixture-signature"},
				{"type": "text", "text": "observed text"},
				{"type": "tool_use", "id": id + "-call", "name": "exec", "input": map[string]any{"code": "text(1)"}},
			}
			for index, block := range blocks {
				events = append(events, map[string]any{"type": "content_block_start", "index": index, "content_block": block})
				if index == 1 && cut == "during_text" {
					events = append(events, map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "text_delta", "text": " prefix"}})
					break
				}
				events = append(events, map[string]any{"type": "content_block_stop", "index": index})
			}
			if cut == "complete" {
				events = append(
					events,
					map[string]any{"type": "message_delta", "delta": map[string]any{"stop_reason": "tool_use"}},
					map[string]any{"type": "message_stop"},
				)
			}
		}
	}
	var stream strings.Builder
	for _, event := range events {
		wire, err := json.Marshal(event)
		if err != nil {
			return nil, err
		}
		fmt.Fprintf(&stream, "data: %s\n\n", wire)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, stream.String())
	}))
	defer server.Close()
	if api == protocol.APIStyleResponses {
		response, err := responses.Sample(t.Context(), server.Client(), server.URL, "Bearer fixture-key", []byte(`{}`),
			responses.Config{Dialect: responses.DialectStandard}, evidence, nil, false, protocol.ProviderHooks{})
		if err != nil {
			return nil, err
		}
		if response.Terminal == nil {
			return nil, errors.New("completed Responses sample has no terminal result")
		}
		records := make([]json.RawMessage, 0, len(response.Output))
		for _, item := range response.Output {
			record, err := json.Marshal(item)
			if err != nil {
				return nil, err
			}
			records = append(records, record)
		}
		return &samplerRecoveryResult{Audit: response.Audit(), Records: records, ContextTokens: response.Terminal.TotalTokens()}, nil
	}
	response, err := anthropic.Sample(t.Context(), server.Client(), server.URL, "fixture-key",
		anthropic.Config{MaxTokens: 1024}, []byte(`{}`), evidence, false, protocol.ProviderHooks{})
	if err != nil {
		return nil, err
	}
	return &samplerRecoveryResult{Audit: response.Audit(), Records: []json.RawMessage{response.Message}, ContextTokens: response.ContextTokens}, nil
}

type samplerProjectedHistory struct {
	items     map[string]*historypb.ThreadItem
	commands  map[string]*historypb.ThreadCommand
	lifecycle []*historypb.ThreadLifecycle
}

func projectSamplerHistory(t *testing.T, fixture samplerExportFixture, sessionID string, sourceCursor uint64) samplerProjectedHistory {
	t.Helper()
	threadID, err := uuid.Parse(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	page, err := fixture.history.ReadPage(t.Context(), threadID, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.GetEntries()) == 0 {
		t.Fatal("History retained no sampler events")
	}
	if page.GetLastCursor() != sourceCursor {
		t.Fatalf("History retained through cursor %d, want recovered sampler cursor %d", page.GetLastCursor(), sourceCursor)
	}
	runner := projection.NewFoldRunner(fixture.historyDB, fixture.history, 1, 1000)
	backlog, err := runner.Cycle(t.Context())
	if err != nil {
		t.Fatalf("project retained sampler history: %v", err)
	}
	var through uint64
	if err := fixture.historyDB.QueryRowContext(t.Context(),
		"SELECT through_cursor FROM thread_checkpoint WHERE thread_id = $1", threadID).Scan(&through); err != nil {
		t.Fatal(err)
	}
	if through != page.GetLastCursor() {
		t.Fatalf("History projected through %d, want committed cursor %d", through, page.GetLastCursor())
	}
	var pending bool
	if err := fixture.historyDB.QueryRowContext(t.Context(),
		"SELECT EXISTS (SELECT 1 FROM session_fold_pending WHERE session_id = $1)", threadID).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending {
		t.Fatalf("History kept sampler session pending after projection: %+v", backlog)
	}

	rows, err := fixture.historyDB.QueryContext(t.Context(), `SELECT entity_kind, entity_id, state FROM thread_entity
		WHERE thread_id = $1 AND projection_epoch = $2`, threadID, projection.FoldEpoch)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	projected := samplerProjectedHistory{
		items: map[string]*historypb.ThreadItem{}, commands: map[string]*historypb.ThreadCommand{},
	}
	for rows.Next() {
		var kind, id string
		var state []byte
		if err := rows.Scan(&kind, &id, &state); err != nil {
			t.Fatal(err)
		}
		switch kind {
		case "item":
			item := &historypb.ThreadItem{}
			if err := protojson.Unmarshal(state, item); err != nil {
				t.Fatal(err)
			}
			projected.items[id] = item
		case "command":
			command := &historypb.ThreadCommand{}
			if err := protojson.Unmarshal(state, command); err != nil {
				t.Fatal(err)
			}
			projected.commands[id] = command
		case "lifecycle":
			lifecycle := &historypb.ThreadLifecycle{}
			if err := protojson.Unmarshal(state, lifecycle); err != nil {
				t.Fatal(err)
			}
			projected.lifecycle = append(projected.lifecycle, lifecycle)
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return projected
}

func samplerHistoryPayload(t *testing.T, fixture samplerExportFixture, sessionID string, reference *historypb.ThreadPayloadReference) string {
	t.Helper()
	if reference == nil || reference.GetField() != historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_OUTPUT {
		t.Fatalf("History output reference=%v", reference)
	}
	threadID, err := uuid.Parse(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := fixture.historyDB.QueryContext(t.Context(), `SELECT text FROM thread_payload_chunk
		WHERE thread_id = $1 AND projection_epoch = $2 AND owner_cursor = $3 AND owner_id = $4
		AND field = 'output' AND generation = $5 AND chunk_index < $6 ORDER BY chunk_index`,
		threadID, reference.GetProjectionEpoch(), reference.GetOwnerCursor(), reference.GetOwnerId(),
		reference.GetGeneration(), reference.GetChunkCount())
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var body strings.Builder
	for rows.Next() {
		var encoded []byte
		var chunk string
		if err := rows.Scan(&encoded); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(encoded, &chunk); err != nil {
			t.Fatal(err)
		}
		body.WriteString(chunk)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return body.String()
}

func TestFailedActiveInputProjectsSeparatelyFromItsTurnAndQueuedInput(t *testing.T) {
	cases := []struct {
		name                 string
		failure              error
		failWithNilCommandID bool
		providerFailure      bool
		wantActive           samplerpb.CommandOutcome
		wantQueued           samplerpb.CommandOutcome
		wantQueueError       bool
		wantReasonContains   string
	}{
		{
			name: "known failure", failure: &protocol.KnownFailureError{Message: "provider rejected request"},
			wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED, wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED,
		},
		{
			name: "permanent provider attempt", providerFailure: true,
			wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED, wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED,
		},
		{
			name: "unknown loss", wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN,
			wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED, wantQueueError: true,
		},
		{
			name: "ordinary execution error", failure: errors.New("unexpected EOF while reading provider stream"),
			wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN, wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED,
			wantQueueError: true, wantReasonContains: "unexpected EOF while reading provider stream",
		},
		{
			name: "successor fails before loading durable active command", failure: errors.New("code host reconciliation failed"),
			failWithNilCommandID: true, wantActive: samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN,
			wantQueued: samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED, wantQueueError: true,
			wantReasonContains: "code host reconciliation failed",
		},
	}
	plans := make([]*samplerExportPlan, len(cases)+2)
	for index := range plans {
		plans[index] = newSamplerExportPlan(0, 0, false)
	}
	fixture := newSamplerExportFixture(t, plans...)

	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			session := fixture.openSession(t)
			scenario, err := fixture.source.StartFailedInputScenario(t.Context(), session)
			if err != nil {
				t.Fatal(err)
			}
			activeID := scenario.ActiveCommandID()
			if activeID != "active-input" {
				t.Fatalf("active command=%q, want active-input", activeID)
			}
			var localCommandID *string
			if !test.failWithNilCommandID {
				localCommandID = &activeID
			}
			if test.providerFailure {
				if err := scenario.FailWithProviderRejection(t.Context(), "provider rejected request"); err != nil {
					t.Fatal(err)
				}
			} else if err := scenario.FailExecution(t.Context(), localCommandID, test.failure); err != nil {
				t.Fatal(err)
			}
			for commandID, want := range map[string]samplerpb.CommandOutcome{
				"active-input": test.wantActive, "queued-input": test.wantQueued,
			} {
				response, err := scenario.CommandResult(t.Context(), commandID)
				if err != nil {
					t.Fatal(err)
				}
				if response.Outcome != want {
					t.Fatalf("GetCommand(%q) outcome=%v, want %v", commandID, response.Outcome, want)
				}
				if commandID == "active-input" && test.wantReasonContains != "" && !strings.Contains(response.FailureReason, test.wantReasonContains) {
					t.Fatalf("GetCommand(%q) failure_reason=%q, want it to contain %q", commandID, response.FailureReason, test.wantReasonContains)
				}
			}
			if test.wantReasonContains != "" {
				blockedReason, err := scenario.BlockedReason(t.Context())
				if err != nil {
					t.Fatal(err)
				}
				if blockedReason == nil || !strings.Contains(*blockedReason, test.wantReasonContains) {
					t.Fatalf("GetSession blocked_reason=%v, want it to contain %q", blockedReason, test.wantReasonContains)
				}
			}
			sourceCursor, err := scenario.Cursor(t.Context())
			if err != nil {
				t.Fatal(err)
			}
			if err := scenario.Release(t.Context()); err != nil {
				t.Fatal(err)
			}

			exportSamplerHistoryThrough(t, fixture, session, sourceCursor)
			projected := projectSamplerHistory(t, fixture, session, sourceCursor)
			activeCommand := projected.commands["active-input"]
			if activeCommand == nil {
				t.Fatal("History did not project the admitted active input")
			}
			if got := activeCommand.GetOutcome(); got != historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_EFFECTED {
				t.Fatalf("confirmed input History outcome=%q, want effected", got)
			}
			wantQueued := historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_PENDING
			if test.wantQueueError {
				wantQueued = historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_FAILED
			}
			queuedCommand := projected.commands["queued-input"]
			if queuedCommand == nil {
				t.Fatal("History did not project the admitted queued input")
			}
			if got := queuedCommand.GetOutcome(); got != wantQueued {
				t.Fatalf("queued input History outcome=%q, want %q", got, wantQueued)
			}
			var failedTurn, failedTurnReason bool
			for _, lifecycle := range projected.lifecycle {
				turn := lifecycle.GetEvent().GetTurnCompleted()
				if turn != nil && turn.GetTurnId() == "active-input" && turn.GetStatus() == eventpb.TurnStatus_TURN_STATUS_FAILED {
					failedTurn = true
					failedTurnReason = test.wantReasonContains == "" || strings.Contains(turn.GetError(), test.wantReasonContains)
				}
			}
			if !failedTurn {
				t.Fatal("History did not retain the failed active turn")
			}
			if !failedTurnReason {
				t.Fatal("History did not retain the original driver error in the failed turn")
			}
		})
	}
}
