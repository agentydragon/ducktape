package storage

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
)

type memoryContent struct {
	byID map[ContentID]StoredContent
}

func newMemoryContent() *memoryContent {
	return &memoryContent{byID: make(map[ContentID]StoredContent)}
}

func (memory *memoryContent) PutContent(kind ContentKind, payload json.RawMessage) (ContentID, error) {
	content, err := CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	if existing, ok := memory.byID[content.ID]; ok && !bytes.Equal(existing.Payload, content.Payload) {
		return "", fmt.Errorf("content ID collision: %s", content.ID)
	}
	memory.byID[content.ID] = content
	return content.ID, nil
}

func (memory *memoryContent) GetContent(id ContentID) (StoredContent, error) {
	content, ok := memory.byID[id]
	if !ok {
		return StoredContent{}, fmt.Errorf("content %s not found", id)
	}
	return content, nil
}

func TestStoreRecordDeduplicatesPartsAndPreservesProviderFields(t *testing.T) {
	memory := newMemoryContent()
	originals := [][]byte{
		[]byte(`{"type":"reasoning","id":"rs_1","status":"completed","role":"assistant","content":[{"type":"output_text","text":"shared payload","provider_part_number":9007199254740993},{"type":"input_image","image_url":"data:image/png;base64,QUJDRA==","detail":"high"}],"summary":[{"type":"summary_text","text":"shared payload"}],"encrypted_content":"opaque-reasoning","provider_sequence":9007199254740993}`),
		[]byte(`{"type":"custom_tool_call","call_id":"call_custom","name":"exec","input":"shared payload","provider_call_number":9007199254740993}`),
		[]byte(`{"type":"function_call","call_id":"call_function","name":"exec","arguments":"shared payload"}`),
		[]byte(`{"type":"function_call_output","call_id":"call_result","output":"shared payload"}`),
		[]byte(`{"type":"message","role":"assistant","output":[{"type":"output_text","text":"shared payload"}]}`),
		[]byte(`{"type":"message","role":"user","content":"shared payload"}`),
	}
	ids := make([]ContentID, 0, len(originals))
	for _, original := range originals {
		id, err := StoreResponsesRecord(memory, original)
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
		roundTripped, err := RestoreResponsesRecord(memory, id)
		if err != nil {
			t.Fatal(err)
		}
		assertResponsesJSONEqual(t, original, roundTripped)
	}

	sharedID := findStringContent(t, memory, ContentText, "shared payload")
	var scalarMessage storedItem
	mustUnmarshal(t, memory.byID[ids[len(ids)-1]].Payload, &scalarMessage)
	if scalarMessage.ContentText == nil || *scalarMessage.ContentText != sharedID {
		t.Fatal("scalar message content did not share text payload")
	}
	if got := countStringContent(memory, ContentText, "shared payload"); got != 1 {
		t.Fatalf("shared text content rows = %d, want one", got)
	}
	if got := countStringContent(memory, ContentOpaque, "opaque-reasoning"); got != 1 {
		t.Fatalf("opaque continuation rows = %d, want one", got)
	}
	if got := countStringContent(memory, ContentImageData, "QUJDRA=="); got != 1 {
		t.Fatalf("image data rows = %d, want one", got)
	}

	var reasoning storedItem
	mustUnmarshal(t, memory.byID[ids[0]].Payload, &reasoning)
	if reasoning.Content == nil || reasoning.Summary == nil || reasoning.EncryptedContent == nil {
		t.Fatalf("reasoning record did not split its repeated content: %+v", reasoning)
	}
	for _, extracted := range []string{"content", "summary", "encrypted_content"} {
		if _, found := reasoning.Envelope[extracted]; found {
			t.Fatalf("extracted field %q remains in provider envelope", extracted)
		}
	}
	if string(reasoning.Envelope["provider_sequence"]) != "9007199254740993" {
		t.Fatalf("provider numeric token changed in envelope: %s", reasoning.Envelope["provider_sequence"])
	}
	var textPart storedPart
	mustUnmarshal(t, memory.byID[(*reasoning.Content)[0]].Payload, &textPart)
	if textPart.Text == nil || *textPart.Text != sharedID {
		t.Fatalf("text part did not use shared text row: %+v", textPart)
	}
	imageID := (*reasoning.Content)[1]
	var imagePart storedPart
	mustUnmarshal(t, memory.byID[imageID].Payload, &imagePart)
	if imagePart.ImageData == nil || imagePart.ImageData.Prefix != "data:image/png;base64," {
		t.Fatalf("image MIME/data URL prefix was not kept as metadata: %+v", imagePart)
	}
	if string(memory.byID[imageID].Payload) == "" || bytes.Contains(memory.byID[imageID].Payload, []byte("QUJDRA==")) {
		t.Fatalf("image base64 was duplicated in its content-part row: %s", memory.byID[imageID].Payload)
	}
	var call storedItem
	mustUnmarshal(t, memory.byID[ids[1]].Payload, &call)
	if call.Input == nil || *call.Input != sharedID {
		t.Fatalf("custom tool input did not share the text row: %+v", call)
	}
	var functionCall storedItem
	mustUnmarshal(t, memory.byID[ids[2]].Payload, &functionCall)
	if functionCall.Arguments == nil || *functionCall.Arguments != sharedID {
		t.Fatalf("function arguments did not share the text row: %+v", functionCall)
	}
	var functionOutput storedItem
	mustUnmarshal(t, memory.byID[ids[3]].Payload, &functionOutput)
	if functionOutput.OutputText == nil || *functionOutput.OutputText != sharedID {
		t.Fatalf("string tool output did not share the text row: %+v", functionOutput)
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
	requestInputs := make([]responses.RequestInput, 0, len(items)+1)
	for _, raw := range items {
		var item responses.Item
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		requestInputs = append(requestInputs, item)
	}
	requestInputs = append(requestInputs, responses.TextToolOutput{Type: responses.ItemFunctionOutput, CallID: "call_result", Output: "shared payload"})
	parameters := json.RawMessage(`{"type":"object","properties":{"code":{"type":"string"}},"required":["code"],"additionalProperties":false}`)
	request := responses.Request{
		Model: "fixture-model", Instructions: "shared payload", Input: requestInputs, Stream: true, Store: false,
		Include: []string{"reasoning.encrypted_content"}, PromptCacheKey: "session-1",
		ClientMetadata:    &responses.ClientMetadata{SessionID: "session-1", ThreadID: "thread-1", TurnID: "turn-1"},
		Reasoning:         &responses.ReasoningConfig{Effort: "high", Summary: "auto", Context: "all_turns"},
		Tools:             []responses.AdditionalTool{{Type: "function", Name: "exec", Description: "Run code", Parameters: parameters}},
		ParallelToolCalls: false,
	}
	profileID, recordIDs, err := StoreResponsesRequest(memory, request)
	if err != nil {
		t.Fatal(err)
	}
	if len(recordIDs) != len(request.Input) {
		t.Fatalf("record count = %d, want %d", len(recordIDs), len(request.Input))
	}
	restoredRecords := make([]json.RawMessage, 0, len(recordIDs))
	for _, id := range recordIDs {
		record, err := RestoreResponsesRecord(memory, id)
		if err != nil {
			t.Fatal(err)
		}
		restoredRecords = append(restoredRecords, record)
	}
	rendered, err := RestoreResponsesRequest(memory, profileID, restoredRecords)
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
	if firstProfile.Instructions != findStringContent(t, memory, ContentText, "shared payload") {
		t.Fatalf("request instructions did not share the existing text content ID: %+v", firstProfile)
	}
	if len(firstProfile.Tools) != 1 {
		t.Fatalf("stored tool refs = %d, want one", len(firstProfile.Tools))
	}
	var tool responses.AdditionalTool
	mustUnmarshal(t, memory.byID[firstProfile.Tools[0]].Payload, &tool)
	if tool.Name != "exec" {
		t.Fatalf("restored tool name = %q, want exec", tool.Name)
	}

	second := request
	metadata := *request.ClientMetadata
	metadata.TurnID = "turn-2"
	second.ClientMetadata = &metadata
	secondID, secondRecords, err := StoreResponsesRequest(memory, second)
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
	profile, refs, err := StoreResponsesRequestJSON(memory, original)
	if err != nil {
		t.Fatal(err)
	}
	if len(refs) != 1 {
		t.Fatalf("historical item count = %d, want 1", len(refs))
	}
	record, err := RestoreResponsesRecord(memory, refs[0])
	if err != nil {
		t.Fatal(err)
	}
	rendered, err := RestoreResponsesRequest(memory, profile, []json.RawMessage{record})
	if err != nil {
		t.Fatal(err)
	}
	assertResponsesJSONEqual(t, original, rendered)
}

func TestRestoreRecordRejectsWrongContentKind(t *testing.T) {
	memory := newMemoryContent()
	id, err := memory.PutContent(ContentText, json.RawMessage(`"not an item"`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RestoreResponsesRecord(memory, id); err == nil {
		t.Fatal("RestoreRecord accepted a text row as an OpenAI item")
	}
}

func TestContentReferencesTraversesOnlyTypedResponsesReferences(t *testing.T) {
	tests := []struct {
		name    string
		kind    ContentKind
		payload string
		want    []ContentID
	}{
		{
			name:    "item",
			kind:    ContentOpenAIItem,
			payload: `{"type":"message","envelope":{"provider_extension":{"ref":"ignored"}},"content_refs":["part-1","part-2"],"summary_refs":["summary-1"],"input_text_ref":"input-1","arguments_text_ref":"args-1","output_text_ref":"output-1","encrypted_content_ref":"opaque-1"}`,
			want:    []ContentID{"part-1", "part-2", "summary-1", "input-1", "args-1", "output-1", "opaque-1"},
		},
		{
			name:    "content part",
			kind:    ContentOpenAIContent,
			payload: `{"type":"input_image","envelope":{"provider_extension":{"ref":"ignored"}},"text_ref":"text-1","image_data":{"field":"image_url","prefix":"data:image/png;base64,","ref":"image-1"}}`,
			want:    []ContentID{"text-1", "image-1"},
		},
		{
			name:    "request profile",
			kind:    ContentOpenAIRequest,
			payload: `{"model":"fixture","instructions_ref":"instructions-1","tool_refs":["tool-1","tool-2"],"envelope":{"provider_extension":{"ref":"ignored"}}}`,
			want:    []ContentID{"instructions-1", "tool-1", "tool-2"},
		},
		{
			name:    "opaque kinds have no traversed refs",
			kind:    ContentOpaque,
			payload: `{"ref":"not-a-graph-edge"}`,
			want:    nil,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := ResponsesContentReferences(test.kind, json.RawMessage(test.payload))
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
	_, err := ResponsesContentReferences(ContentOpenAIRequest, json.RawMessage(`{"instructions_ref":""}`))
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

func findStringContent(t *testing.T, memory *memoryContent, kind ContentKind, expected string) ContentID {
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

func countStringContent(memory *memoryContent, kind ContentKind, expected string) int {
	count := 0
	for _, content := range memory.byID {
		if content.Kind != kind {
			continue
		}
		var value string
		if err := json.Unmarshal(content.Payload, &value); err == nil && value == expected {
			count++
		}
	}
	return count
}

func mustUnmarshal(t *testing.T, raw []byte, target any) {
	t.Helper()
	if err := json.Unmarshal(raw, target); err != nil {
		t.Fatal(err)
	}
}
