package responses

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

type memoryContent struct {
	byID map[storage.ContentID]storage.StoredContent
}

func newMemoryContent() *memoryContent {
	return &memoryContent{byID: make(map[storage.ContentID]storage.StoredContent)}
}

func (memory *memoryContent) PutContent(kind storage.ContentKind, payload json.RawMessage) (storage.ContentID, error) {
	content, err := storage.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	if existing, ok := memory.byID[content.ID]; ok && !bytes.Equal(existing.Payload, content.Payload) {
		return "", fmt.Errorf("content ID collision: %s", content.ID)
	}
	memory.byID[content.ID] = content
	return content.ID, nil
}

func (memory *memoryContent) GetContent(id storage.ContentID) (storage.StoredContent, error) {
	content, ok := memory.byID[id]
	if !ok {
		return storage.StoredContent{}, fmt.Errorf("content %s not found", id)
	}
	return content, nil
}

func TestStoreRecordSharesCompleteNativeItemsAndPreservesProviderFields(t *testing.T) {
	memory := newMemoryContent()
	originals := []json.RawMessage{
		json.RawMessage(`{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text","text":"shared payload"}],"encrypted_content":"opaque-reasoning","provider_sequence":9007199254740993}`),
		json.RawMessage(`{"type":"custom_tool_call","call_id":"call_custom","name":"exec","input":"shared payload","provider_call_number":9007199254740993}`),
		json.RawMessage(`{"type":"function_call","call_id":"call_function","name":"exec","arguments":"{\"code\":\"shared payload\"}"}`),
		json.RawMessage(`{"type":"function_call_output","call_id":"call_result","output":"shared payload"}`),
		json.RawMessage(`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"shared payload","provider_part_number":9007199254740993},{"type":"input_image","image_url":"data:image/png;base64,QUJDRA==","detail":"high"}]}`),
		json.RawMessage(`{"type":"message","role":"user","content":"shared payload"}`),
	}
	for index, original := range originals {
		id, err := StoreRecord(memory, original)
		if err != nil {
			t.Fatal(err)
		}
		assertResponsesJSONEqual(t, original, memory.byID[id].Payload)
		roundTripped, err := RestoreRecord(memory, id)
		if err != nil {
			t.Fatal(err)
		}
		assertResponsesJSONEqual(t, original, roundTripped)
		repeatedID, err := StoreRecord(memory, original)
		if err != nil {
			t.Fatal(err)
		}
		if repeatedID != id || len(memory.byID) != index+1 {
			t.Fatalf("repeated item added storage: first=%s second=%s rows=%d", id, repeatedID, len(memory.byID))
		}
	}
}

func TestStoreRequestSharesProfileContentAcrossCallsAndRendersNativeRequest(t *testing.T) {
	memory := newMemoryContent()
	items := []json.RawMessage{
		json.RawMessage(`{"type":"reasoning","id":"rs_1","summary":[{"type":"summary_text","text":"shared payload"}],"encrypted_content":"opaque","provider_number":9007199254740993}`),
		json.RawMessage(`{"type":"custom_tool_call","call_id":"call_custom","name":"exec","input":"shared payload"}`),
		json.RawMessage(`{"type":"function_call","call_id":"call_function","name":"exec","arguments":"shared payload"}`),
		json.RawMessage(`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"shared payload"},{"type":"input_image","image_url":"data:image/jpeg;base64,QUJDRA==","detail":"auto"}]}`),
	}
	requestInputs := make([]RequestInput, 0, len(items)+1)
	for _, raw := range items {
		var item Item
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		requestInputs = append(requestInputs, item)
	}
	requestInputs = append(requestInputs, TextToolOutput{Type: ItemFunctionOutput, CallID: "call_result", Output: "shared payload"})
	parameters := json.RawMessage(`{"type":"object","properties":{"code":{"type":"string"}},"required":["code"],"additionalProperties":false}`)
	request := Request{
		Model: "fixture-model", Instructions: "shared payload", Input: requestInputs, Stream: true, Store: false,
		Include: []string{"reasoning.encrypted_content"}, PromptCacheKey: "session-1",
		ClientMetadata:    &ClientMetadata{SessionID: "session-1", ThreadID: "thread-1", TurnID: "turn-1"},
		Reasoning:         &ReasoningConfig{Effort: "high", Summary: "auto", Context: "all_turns"},
		Tools:             []AdditionalTool{{Type: "function", Name: "exec", Description: "Run code", Parameters: parameters}},
		ParallelToolCalls: false,
	}
	profileID, recordIDs, err := StoreRequest(memory, request)
	if err != nil {
		t.Fatal(err)
	}
	if len(recordIDs) != len(request.Input) {
		t.Fatalf("record count = %d, want %d", len(recordIDs), len(request.Input))
	}
	restoredRecords := make([]json.RawMessage, 0, len(recordIDs))
	for _, id := range recordIDs {
		record, err := RestoreRecord(memory, id)
		if err != nil {
			t.Fatal(err)
		}
		restoredRecords = append(restoredRecords, record)
	}
	rendered, err := RestoreRequest(memory, profileID, restoredRecords)
	if err != nil {
		t.Fatal(err)
	}
	want, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	assertResponsesJSONEqual(t, want, rendered)

	var firstProfile storedRequestProfile
	mustUnmarshal(t, memory.byID[profileID].Payload, &firstProfile)
	if firstProfile.Instructions != findStringContent(t, memory, storage.ContentText, "shared payload") {
		t.Fatalf("request instructions did not share the existing text content ID: %+v", firstProfile)
	}
	if len(firstProfile.Tools) != 1 {
		t.Fatalf("stored tool refs = %d, want one", len(firstProfile.Tools))
	}
	var tool AdditionalTool
	mustUnmarshal(t, memory.byID[firstProfile.Tools[0]].Payload, &tool)
	if tool.Name != "exec" {
		t.Fatalf("restored tool name = %q, want exec", tool.Name)
	}

	second := request
	metadata := *request.ClientMetadata
	metadata.TurnID = "turn-2"
	second.ClientMetadata = &metadata
	secondID, secondRecords, err := StoreRequest(memory, second)
	if err != nil {
		t.Fatal(err)
	}
	var secondProfile storedRequestProfile
	mustUnmarshal(t, memory.byID[secondID].Payload, &secondProfile)
	if secondID == profileID {
		t.Fatal("per-call client metadata did not produce a distinct profile")
	}
	if secondProfile.Instructions != firstProfile.Instructions || !reflect.DeepEqual(secondProfile.Tools, firstProfile.Tools) {
		t.Fatalf("per-call metadata duplicated profile content: first=%+v second=%+v", firstProfile, secondProfile)
	}
	if !reflect.DeepEqual(secondRecords, recordIDs) {
		t.Fatalf("same native items got different content IDs: first=%v second=%v", recordIDs, secondRecords)
	}
}

func TestStoreRequestJSONPreservesHistoricalUnknownEnvelopes(t *testing.T) {
	memory := newMemoryContent()
	original := json.RawMessage(`{"model":"fixture-model","instructions":"legacy instructions","input":[{"type":"reasoning","encrypted_content":"opaque-legacy","summary":[{"type":"summary_text","text":"keep this"}],"provider_number":9007199254740993}],"tools":[{"type":"function","name":"exec","description":"Run","parameters":{"type":"object"},"provider_tool_extension":{"revision":9007199254740993}}],"stream":true,"store":false,"prompt_cache_key":"session","parallel_tool_calls":false,"provider_extension":{"sequence":9007199254740993}}`)
	profile, refs, err := StoreRequestJSON(memory, original)
	if err != nil {
		t.Fatal(err)
	}
	if len(refs) != 1 {
		t.Fatalf("historical item count = %d, want 1", len(refs))
	}
	record, err := RestoreRecord(memory, refs[0])
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := RestoreRequest(memory, profile, []json.RawMessage{record})
	if err != nil {
		t.Fatal(err)
	}
	assertResponsesJSONEqual(t, original, rendered)
}

func TestRestoreRecordRejectsWrongContentKind(t *testing.T) {
	memory := newMemoryContent()
	id, err := memory.PutContent(storage.ContentText, json.RawMessage(`"not an item"`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RestoreRecord(memory, id); err == nil {
		t.Fatal("RestoreRecord accepted a text row as an OpenAI item")
	}
}

func TestContentReferencesTraversesOnlyTypedResponsesReferences(t *testing.T) {
	tests := []struct {
		name    string
		kind    storage.ContentKind
		payload string
		want    []storage.ContentID
	}{
		{
			name:    "native item fields are not storage references",
			kind:    ContentOpenAIItem,
			payload: `{"type":"message","role":"user","content":[{"type":"input_text","text":"prompt"}],"content_refs":["provider-owned"],"provider_extension":{"ref":"ignored"}}`,
			want:    nil,
		},
		{
			name:    "request profile",
			kind:    ContentOpenAIRequest,
			payload: `{"model":"fixture","instructions_ref":"instructions-1","tool_refs":["tool-1","tool-2"],"envelope":{"provider_extension":{"ref":"ignored"}}}`,
			want:    []storage.ContentID{"instructions-1", "tool-1", "tool-2"},
		},
		{
			name:    "tool definitions have no traversed refs",
			kind:    ContentOpenAITool,
			payload: `{"ref":"not-a-graph-edge"}`,
			want:    nil,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := ContentReferences(test.kind, json.RawMessage(test.payload))
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("ContentReferences() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestContentReferencesRejectsEmptyReference(t *testing.T) {
	_, err := ContentReferences(ContentOpenAIRequest, json.RawMessage(`{"instructions_ref":""}`))
	if err == nil {
		t.Fatal("ContentReferences accepted an empty instructions ID")
	}
}

func assertResponsesJSONEqual(t *testing.T, want, got []byte) {
	t.Helper()
	wantValue := decodeJSON(t, want)
	gotValue := decodeJSON(t, got)
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Fatalf("JSON differs\n got: %s\nwant: %s", got, want)
	}
}

func decodeJSON(t *testing.T, raw []byte) any {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func findStringContent(t *testing.T, memory *memoryContent, kind storage.ContentKind, expected string) storage.ContentID {
	t.Helper()
	for id, content := range memory.byID {
		if content.Kind != kind {
			continue
		}
		var value string
		if err := json.Unmarshal(content.Payload, &value); err == nil && value == expected {
			return id
		}
	}
	t.Fatalf("no %s row contains %q", kind, expected)
	return ""
}

func mustUnmarshal(t *testing.T, raw []byte, target any) {
	t.Helper()
	if err := json.Unmarshal(raw, target); err != nil {
		t.Fatal(err)
	}
}

func TestIncrementalProjectionRecognizesOutputsInRetainedPrefix(t *testing.T) {
	for _, dialect := range []Dialect{DialectLite, DialectStandard} {
		for _, encoding := range []protocol.ExecEncoding{protocol.ExecEncodingJSON, protocol.ExecEncodingFreeform} {
			t.Run(string(dialect)+"/"+string(encoding), func(t *testing.T) {
				state := &Context{}
				if err := state.AppendRecords([]json.RawMessage{
					json.RawMessage(`{"type":"function_call","call_id":"call_1","name":"exec","arguments":"{}"}`),
					json.RawMessage(`{"type":"function_call_output","call_id":"call_1","output":[{"type":"input_text","text":"first"}]}`),
					json.RawMessage(`{"type":"function_call_output","call_id":"call_1","output":[{"type":"input_text","text":"later"}]}`),
				}); err != nil {
					t.Fatal(err)
				}
				all, err := state.ProjectRecords(0, Config{Dialect: dialect}, encoding)
				if err != nil {
					t.Fatal(err)
				}
				suffix, err := state.ProjectRecords(2, Config{Dialect: dialect}, encoding)
				if err != nil {
					t.Fatal(err)
				}
				if len(suffix) != 1 {
					t.Fatalf("suffix has %d records", len(suffix))
				}
				assertResponsesJSONEqual(t, all[2], suffix[0])
				var item Item
				if err := json.Unmarshal(suffix[0], &item); err != nil {
					t.Fatal(err)
				}
				if (dialect == DialectStandard || encoding == protocol.ExecEncodingJSON) && (item.Type != ItemMessage || item.Role != "developer") {
					t.Fatal("later output did not become a developer notice")
				}
			})
		}
	}
}
