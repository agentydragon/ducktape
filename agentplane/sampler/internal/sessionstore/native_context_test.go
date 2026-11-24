package sessionstore

import (
	"bytes"
	"encoding/json"
	"errors"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestToolResultsResolveTranscriptProvenanceInCompactedContext(t *testing.T) {
	for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
		t.Run(string(api), func(t *testing.T) {
			transcript, err := newNativeContext(api)
			if err != nil {
				t.Fatal(err)
			}
			if err := transcript.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "earlier turn"}}); err != nil {
				t.Fatal(err)
			}
			completed := json.RawMessage(`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"earlier answer"}]}`)
			callRecord := json.RawMessage(`{"type":"function_call","call_id":"call_fixture","name":"exec","arguments":"{\"code\":\"text(1)\"}"}`)
			summary := json.RawMessage(`{"type":"compaction","encrypted_content":"opaque-fixture"}`)
			if api == protocol.APIStyleAnthropicMessages {
				completed = json.RawMessage(`{"id":"msg_fixture_earlier","role":"assistant","content":[{"type":"text","text":"earlier answer"}],"stop_reason":"end_turn"}`)
				callRecord = json.RawMessage(`{"id":"msg_fixture_call","role":"assistant","content":[{"type":"tool_use","id":"call_fixture","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use"}`)
				summary = json.RawMessage(`{"id":"msg_fixture_summary","role":"assistant","content":[{"type":"compaction","content":"earlier summary","signature":"fixture-signature"}],"stop_reason":"compaction"}`)
			}
			if err := transcript.AppendRecords([]json.RawMessage{completed}); err != nil {
				t.Fatal(err)
			}
			if err := transcript.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "current turn"}}); err != nil {
				t.Fatal(err)
			}
			if err := transcript.AppendRecords([]json.RawMessage{callRecord}); err != nil {
				t.Fatal(err)
			}
			active, err := newNativeContext(api)
			if err != nil {
				t.Fatal(err)
			}
			if err := active.AppendRecords([]json.RawMessage{summary}); err != nil {
				t.Fatal(err)
			}
			if err := active.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "current turn"}}); err != nil {
				t.Fatal(err)
			}
			if err := active.AppendRecords([]json.RawMessage{callRecord}); err != nil {
				t.Fatal(err)
			}
			conversation := &Conversation{API: api, Transcript: transcript, ActiveContext: active}
			before, err := json.Marshal(transcript)
			if err != nil {
				t.Fatal(err)
			}
			calls, err := transcript.PendingCalls()
			if err != nil || len(calls) != 1 {
				t.Fatalf("pending calls: %v %v", calls, err)
			}
			after, err := json.Marshal(transcript)
			if err != nil || string(before) != string(after) {
				t.Fatalf("deriving execution decisions changed native records: %v", err)
			}
			call := calls[0]
			activeCall, err := active.FindCall(call.CallID)
			if err != nil || call.SourceRecord == activeCall.SourceRecord {
				t.Fatalf("fixture must exercise different source ordinals: %v %v %v", call, activeCall, err)
			}
			forged := call
			forged.SourceRecord++
			if err := conversation.appendToolResult(forged, []protocol.ContentPart{{Type: "input_text", Text: "result"}}); !errors.Is(err, protocol.ErrInvalid) {
				t.Fatalf("incorrect source record was accepted: %v", err)
			}
			if err := conversation.appendToolResult(call, []protocol.ContentPart{{Type: "input_text", Text: "result"}}); err != nil {
				t.Fatal(err)
			}
			for _, state := range []protocol.Context{transcript, active} {
				if err := state.CheckSettled(); err != nil {
					t.Fatalf("result did not settle the native call: %v", err)
				}
				records, err := state.Records()
				if err != nil {
					t.Fatal(err)
				}
				var last map[string]json.RawMessage
				if err := json.Unmarshal(records[len(records)-1], &last); err != nil {
					t.Fatal(err)
				}
				if api == protocol.APIStyleAnthropicMessages {
					var role string
					if err := json.Unmarshal(last["role"], &role); err != nil || role != "user" {
						t.Fatalf("Anthropic tool result was not a native user message: %s (role %q, err %v)", records[len(records)-1], role, err)
					}
				}
				if api == protocol.APIStyleResponses {
					var itemType string
					if err := json.Unmarshal(last["type"], &itemType); err != nil || itemType != "function_call_output" {
						t.Fatalf("Responses result has wrong native type: %s (type %q, err %v)", records[len(records)-1], itemType, err)
					}
				}
			}
		})
	}
}

func TestCompactionNotificationsRetainAnsweredCallProvenanceAfterReload(t *testing.T) {
	for api, modelCall := range map[protocol.APIStyle]json.RawMessage{
		protocol.APIStyleResponses:         json.RawMessage(`{"type":"function_call","call_id":"async-call","name":"exec","arguments":"{\"code\":\"text(1)\"}"}`),
		protocol.APIStyleAnthropicMessages: json.RawMessage(`{"id":"msg_async","role":"assistant","content":[{"type":"tool_use","id":"async-call","name":"exec","input":{"code":"text(1)"}}],"stop_reason":"tool_use"}`),
		protocol.APIStyleChatCompletions:   json.RawMessage(`{"role":"assistant","tool_calls":[{"id":"async-call","type":"function","function":{"name":"exec","arguments":"{\"code\":\"text(1)\"}"}}]}`),
	} {
		t.Run(string(api), func(t *testing.T) {
			ctx := t.Context()
			store, dsn := newTestStoreWithDSN(t)
			request := &sessionconfig.Configuration{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Model: "fixture-model", Cwd: "/fixture"}}
			session, err := openStoreSession(t, store, request, "fixture-account", api)
			if err != nil {
				t.Fatal(err)
			}
			owner := claim(t, store, session)
			admitInput(t, store, session.ID, "turn", "run the cell")
			if _, err := store.NextCommand(ctx, owner, session.ID); err != nil {
				t.Fatal(err)
			}
			sampling, _, err := store.Begin(ctx, owner, IntentPayload{Kind: PendingIntentSampling}, nil, providerAttemptTestPolicyPointer())
			if err != nil {
				t.Fatal(err)
			}
			if err := store.Finish(ctx, owner, sampling, map[string]any{}, []json.RawMessage{modelCall}, nil); err != nil {
				t.Fatal(err)
			}
			if err := store.QueueNotification(ctx, owner, "async-call", "live-cell", "before summary started"); err != nil {
				t.Fatal(err)
			}
			if drained, err := store.DrainNotifications(ctx, owner); err == nil || drained {
				t.Fatalf("drain with unanswered tool: drained=%v err=%v", drained, err)
			}
			conversation, err := store.Read(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			call, err := conversation.Transcript.FindCall("async-call")
			if err != nil {
				t.Fatal(err)
			}
			cell, _, err := store.Begin(ctx, owner, IntentPayload{Kind: PendingIntentCell, Call: &call}, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.FinishTool(ctx, owner, cell, map[string]any{}, call, []protocol.ContentPart{{Type: "input_text", Text: "yielded live-cell"}}); err != nil {
				t.Fatal(err)
			}
			conversation, err = store.Read(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			origins := conversation.Transcript.Origins()
			if origins.OriginAt(call.SourceRecord-1) != (protocol.RecordOrigin{Kind: protocol.RecordOriginProviderResult, SourceID: sampling}) ||
				origins.OriginAt(conversation.Transcript.RecordCount()-1) != (protocol.RecordOrigin{Kind: protocol.RecordOriginToolResult, SourceID: cell}) {
				t.Fatal("the accepted call and tool result did not retain their own intent IDs")
			}
			compaction, _, err := store.BeginCompaction(ctx, owner, IntentPayload{Kind: PendingIntentCompaction}, conversation.Context(), nil, providerAttemptTestPolicy())
			if err != nil {
				t.Fatal(err)
			}
			if err := store.QueueNotification(ctx, owner, "async-call", "live-cell", "while summary ran"); err != nil {
				t.Fatal(err)
			}
			store = New(postgres.Open(t, dsn), store.operationTimeout, nil)
			replacement, err := newNativeContext(api)
			if err != nil {
				t.Fatal(err)
			}
			if err := replacement.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: "compacted summary"}}); err != nil {
				t.Fatal(err)
			}
			if err := store.FinishCompaction(ctx, owner, compaction, map[string]any{}, conversation.Context(), replacement, nil); err != nil {
				t.Fatal(err)
			}
			if replacement.RecordCount() != 1 {
				t.Fatal("persistence mutated the replacement that a retry would reuse")
			}
			if err := store.QueueNotification(ctx, owner, "async-call", "live-cell", "after replacement committed"); err != nil {
				t.Fatal(err)
			}
			if drained, err := store.DrainNotifications(ctx, owner); err != nil || !drained {
				t.Fatalf("drain after compaction: drained=%v err=%v", drained, err)
			}
			if drained, err := store.DrainNotifications(ctx, owner); err != nil || drained {
				t.Fatalf("second drain: drained=%v err=%v", drained, err)
			}
			conversation, err = store.Read(ctx, owner)
			if err != nil {
				t.Fatal(err)
			}
			if got, err := conversation.Transcript.FindCall("async-call"); err != nil || got != call {
				t.Fatalf("originating transcript call changed: call=%+v err=%v", got, err)
			}
			if _, err := conversation.Context().FindCall("async-call"); !errors.Is(err, protocol.ErrMissing) {
				t.Fatalf("compacted context should have a notice without a tool call: %v", err)
			}
			for _, native := range []protocol.Context{conversation.Transcript, conversation.Context()} {
				if err := native.CheckSettled(); err != nil {
					t.Fatal(err)
				}
				wire, err := json.Marshal(native)
				if err != nil {
					t.Fatal(err)
				}
				for _, marker := range []string{"before summary started", "while summary ran", "after replacement committed"} {
					if count := bytes.Count(wire, []byte(marker)); count != 1 {
						t.Fatalf("%q occurred %d times in context %s", marker, count, wire)
					}
				}
			}
			if err := store.QueueNotification(ctx, owner, "unknown-call", "live-cell", "untrusted origin"); err != nil {
				t.Fatal(err)
			}
			if drained, err := store.DrainNotifications(ctx, owner); drained || !errors.Is(err, protocol.ErrMissing) {
				t.Fatalf("notification with unknown transcript origin: drained=%v err=%v", drained, err)
			}
		})
	}
}

func TestReopeningSessionCannotChangeItsNativeAPI(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	request := &sessionconfig.Configuration{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		t.Fatal(err)
	}
	if _, err := openStoreSession(t, store, request, session.OwnerKey, protocol.APIStyleAnthropicMessages); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("reopening a Responses conversation as Anthropic: %v", err)
	}
	if _, err := openStoreSession(t, store, request, session.OwnerKey, protocol.APIStyleResponses); err != nil {
		t.Fatalf("reopening the original protocol: %v", err)
	}
}
