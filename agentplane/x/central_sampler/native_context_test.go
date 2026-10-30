package centralsampler

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/protobuf/proto"
)

func TestToolResultsResolveArchiveProvenanceInCompactedContext(t *testing.T) {
	for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
		t.Run(string(api), func(t *testing.T) {
			archive, err := newNativeContext(api)
			if err != nil {
				t.Fatal(err)
			}
			if err := archive.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "earlier turn"}}); err != nil {
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
			if err := archive.AppendRecords([]json.RawMessage{completed}); err != nil {
				t.Fatal(err)
			}
			if err := archive.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "current turn"}}); err != nil {
				t.Fatal(err)
			}
			if err := archive.AppendRecords([]json.RawMessage{callRecord}); err != nil {
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
			conversation := &Conversation{API: api, Archive: archive, ActiveContext: active}
			before, err := json.Marshal(archive)
			if err != nil {
				t.Fatal(err)
			}
			calls, err := archive.PendingCalls()
			if err != nil || len(calls) != 1 {
				t.Fatalf("pending calls: %v %v", calls, err)
			}
			after, err := json.Marshal(archive)
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
			for _, state := range []protocol.Context{archive, active} {
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

func TestReopeningSessionCannotChangeItsNativeAPI(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	request := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		t.Fatal(err)
	}
	if _, err := store.OpenSession(t.Context(), request, session.AccountUID, protocol.APIStyleAnthropicMessages); !errors.Is(err, protocol.ErrPrecondition) {
		t.Fatalf("reopening a Responses conversation as Anthropic: %v", err)
	}
	if _, err := store.OpenSession(t.Context(), request, session.AccountUID, protocol.APIStyleResponses); err != nil {
		t.Fatalf("reopening the original protocol: %v", err)
	}
}
